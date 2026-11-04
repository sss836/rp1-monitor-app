import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';

// These pure helpers are also tested with Three.js, independent of WebGL or Qt.
export function jointQuaternion(base, axis, angle) {
  if (base.length !== 4 || axis.length !== 3 || [...base, ...axis].some(v => !Number.isFinite(v))) throw new Error('invalid joint transform');
  const a = new THREE.Vector3(...axis);
  if (!Number.isFinite(angle) || !a.lengthSq()) throw new Error('invalid joint angle or axis');
  return new THREE.Quaternion(...base).multiply(new THREE.Quaternion().setFromAxisAngle(a.normalize(), angle));
}
export function baselinePose(metadata, plan) {
  if (plan && ![24, 28].includes(plan.dof)) throw new Error('unknown model DOF');
  const names = plan?.joint_names || [];
  const values = plan?.initial_pose_rad;
  if (plan && (names.length !== plan.dof || new Set(names).size !== names.length)) throw new Error('invalid model joint names');
  if (values && (values.length !== names.length || values.some(q => !Number.isFinite(q)))) throw new Error('invalid initial pose');
  const active = new Set(names);
  const pose = new Map();
  for (const row of metadata.joints) {
    const i = names.indexOf(row.name);
    const q = i >= 0 && values ? values[i] : 0;
    if (i >= 0 && (q < row.lower - 1e-7 || q > row.upper + 1e-7)) throw new Error('initial pose outside URDF range');
    // Four extra yaw/pitch wrist axes stay at zero in the 24-joint view.
    pose.set(row.name, active.has(row.name) ? q : 0);
  }
  if (names.some(name => !pose.has(name))) throw new Error('plan joint missing in URDF');
  return pose;
}
export function endpoint(row, metadata, plan) {
  if (!row) return null;
  const joint = metadata.joints.find(j => j.name === row.joint);
  if (!joint || !plan?.joint_names?.includes(row.joint)) throw new Error('selected joint absent from selected model');
  const q = row.reference_rad ?? row.requested_rad;
  if (row.direction === 'center' && ['waist_roll_joint', 'left_hip_pitch_joint', 'right_hip_pitch_joint'].includes(row.joint) && q === 0) return { joint, angle: 0, pinZero: row.joint === 'waist_roll_joint', centerZero: true };
  if (!['lower', 'upper'].includes(row.direction) || !Number.isFinite(q)) throw new Error('unknown endpoint');
  const limit = row.direction === 'lower' ? joint.lower : joint.upper;
  if (Math.abs(limit - q) > 1e-5) throw new Error('reference differs from URDF endpoint');
  return { joint, angle: q };
}
export function animatedAngle(start, target, seconds) {
  // A complete, smooth out-and-back motion includes a short endpoint dwell.
  const cycle = ((seconds % 6) + 6) % 6;
  let fraction = cycle < 2.5 ? cycle / 2.5 : cycle < 3.3 ? 1 : cycle < 5.8 ? 1 - (cycle - 3.3) / 2.5 : 0;
  fraction = fraction * fraction * (3 - 2 * fraction);
  return start + (target - start) * fraction;
}
export function jointTitle(name) {
  const side = name.startsWith('left_') ? '左' : name.startsWith('right_') ? '右' : '';
  const part = [['hip', '髋'], ['knee', '膝'], ['ankle', '踝'], ['waist', '腰'],
    ['shoulder', '肩'], ['elbow', '肘'], ['wrist', '腕']].find(([token]) => name.includes(token));
  const axis = name.includes('pitch') ? '前后摆' : name.includes('roll') ? '侧摆' : name.includes('yaw') ? '旋转' : '屈伸';
  return part ? `${side}${part[1]} · ${axis}` : name;
}

function mount() {
  const el = id => document.getElementById(id);
  const host = el('canvas');
  let renderer, camera, scene, controls, environment, rig, metadata;
  let frame = 0, lastTime = 0, elapsed = 0, paused = matchMedia('(prefers-reduced-motion: reduce)').matches;
  let ready = false, closed = false, interaction = false, pending = { plan: null, outcome: null };
  let signature = '', selected = null, pose = new Map(), marker, homePosition, homeTarget;
  const nodes = new Map(), bases = new Map(), originalMaterials = new Map();
  const root = new THREE.Group();
  root.rotation.x = -Math.PI / 2; // ROS metres / Z-up -> Three Y-up, outside URDF pivots.
  const clock = () => { lastTime = 0; };
  const setStatus = value => { el('status').textContent = value; };
  const setPauseLabel = () => { el('pause').textContent = paused ? '播放动画' : '暂停动画'; el('pause').setAttribute('aria-pressed', String(paused)); };
  const grey = new THREE.MeshStandardMaterial({ color: 0x8393a6, metalness: .25, roughness: .48 });
  const blue = new THREE.MeshStandardMaterial({ color: 0x2673bd, metalness: .3, roughness: .4,
    emissive: 0x0c2648, emissiveIntensity: .22 });
  const dark = new THREE.MeshStandardMaterial({ color: 0x354758, metalness: .55, roughness: .43 });
  const brand = new THREE.MeshStandardMaterial({ color: 0x699bc8, metalness: .36, roughness: .35 });

  function applyPose(name, q) {
    const node = nodes.get(name), data = metadata.joints.find(row => row.name === name);
    if (node && data) node.quaternion.copy(jointQuaternion(bases.get(name), data.axis, q));
  }
  function resetPose() {
    for (const [name, q] of pose) applyPose(name, q);
    root.updateMatrixWorld(true);
  }
  function highlight() {
    for (const [mesh, material] of originalMaterials) mesh.material = material;
    marker.visible = false;
    if (!selected) return;
    const node = nodes.get(selected.joint.name);
    node.traverse(item => { if (item.isMesh) item.material = blue; });
    node.getWorldPosition(marker.position);
    marker.visible = true;
  }
  function boundsForSelection() {
    const bounds = new THREE.Box3();
    if (!selected) return bounds.setFromObject(root);
    const name = selected.joint.name, start = pose.get(name);
    // Frame the entire demonstrated arc, avoiding clipping at the selected limit.
    for (let i = 0; i <= 12; i++) {
      applyPose(name, start + (selected.angle - start) * i / 12);
      root.updateMatrixWorld(true);
      bounds.union(new THREE.Box3().setFromObject(root));
    }
    resetPose();
    return bounds;
  }
  function resetCamera() {
    if (!ready) return;
    const bounds = boundsForSelection();
    const target = bounds.getCenter(new THREE.Vector3());
    let direction = new THREE.Vector3(2.3, .65, 1.2);
    if (selected) {
      const node = nodes.get(selected.joint.name), rotation = node.getWorldQuaternion(new THREE.Quaternion());
      direction = new THREE.Vector3(...selected.joint.axis).applyQuaternion(rotation).normalize();
      // Viewing along the physical joint axis exposes the real rotation plane.
      if (Math.abs(direction.y) > .75) direction.set(.28, 1, .38);
      else { if (direction.x < -.1 || direction.z < -.1) direction.negate(); direction.y += .18; }
    }
    direction.normalize();
    const sphere = bounds.getBoundingSphere(new THREE.Sphere());
    const radius = Math.max(.2, sphere.radius);
    const vertical = THREE.MathUtils.degToRad(camera.fov) / 2;
    const horizontal = Math.atan(Math.tan(vertical) * camera.aspect);
    const distance = radius / Math.sin(Math.min(vertical, horizontal)) * 1.14;
    controls.target.copy(target);
    camera.position.copy(target).addScaledVector(direction, distance);
    controls.minDistance = .18;
    controls.maxDistance = Math.max(8, distance * 3);
    camera.near = .01; camera.far = 100; camera.updateProjectionMatrix();
    controls.update();
    homePosition = camera.position.clone(); homeTarget = controls.target.clone();
    // Physical model remains untouched; ground sits below its neutral sole.
    if (!selected) setStatus('拖动旋转 · 滚轮缩放 · 选择关节查看所选单侧动作');
    else setStatus(selected.centerZero ? '蓝色：当前关节 · 零位±2°保持5秒，由实测判定' : '蓝色：当前关节及其子链 · 固定 ±3°由实测判定');
  }
  function configure(value) {
    if (closed) return;
    pending = value || { plan: null, outcome: null };
    if (!ready) return;
    const next = JSON.stringify(pending);
    if (next === signature) return; // Incoming telemetry must never restart the illustrative animation.
    try {
      if (pending.plan?.urdf_sha256 && pending.plan.urdf_sha256 !== metadata.urdf_sha256) throw new Error('URDF source mismatch');
      pose = baselinePose(metadata, pending.plan);
      selected = endpoint(pending.outcome, metadata, pending.plan);
      signature = next;
      resetPose(); highlight(); elapsed = 0; clock(); resetCamera();
      const row = pending.outcome;
      el('joint').textContent = row ? jointTitle(row.joint) : 'RP1 · 真实 URDF 模型';
      const names = { lower: '下限', upper: '上限' };
      const zero = row?.check_side_basis?.includes('center') ? '居中' : names[row?.zero_direction] || '未知';
      el('side').textContent = selected?.centerZero ? `${selected.pinZero ? '腰部插销定位' : '髋关节零位'} · 模型零位 0° · 允许 ±2°` : row ? `标零侧：${zero} · 本次：${names[row.direction]} · 模拟参考 ${(selected.angle * 180 / Math.PI).toFixed(1)}°` : '选择24或28电机机型与检查关节';
    } catch (error) { fail('参考与模型不一致：' + error.message); }
  }
  function resize() {
    if (!renderer || closed) return;
    const width = host.clientWidth, height = host.clientHeight;
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height; camera.updateProjectionMatrix();
    if (ready) resetCamera();
  }
  function render(time) {
    frame = 0;
    if (closed || !ready || document.hidden) { clock(); return; }
    if (!paused && !interaction && selected) {
      if (lastTime) elapsed += Math.min((time - lastTime) / 1000, .08);
      applyPose(selected.joint.name, animatedAngle(pose.get(selected.joint.name), selected.angle, elapsed));
      root.updateMatrixWorld(true);
      nodes.get(selected.joint.name).getWorldPosition(marker.position);
    }
    lastTime = time;
    controls.update(); renderer.render(scene, camera);
    frame = requestAnimationFrame(render);
  }
  function startFrames() { if (!frame && ready && !closed && !document.hidden) frame = requestAnimationFrame(render); }
  function shutdown() {
    if (closed) return;
    closed = true; ready = false;
    cancelAnimationFrame(frame); frame = 0;
    controls?.dispose(); observer?.disconnect();
    const geometries = new Set(), materials = new Set();
    scene?.traverse(item => {
      if (item.geometry) geometries.add(item.geometry);
      for (const material of Array.isArray(item.material) ? item.material : [item.material]) if (material) materials.add(material);
    });
    for (const material of [grey, blue, dark, brand]) materials.add(material);
    for (const value of [...geometries, ...materials]) value.dispose();
    environment?.dispose(); renderer?.dispose(); renderer?.domElement.remove();
  }
  function fail(message) {
    shutdown();
    el('loading').hidden = true; el('fallback').hidden = false;
    setStatus('三维模型不可用：' + message + ' · 静态图不演示动作');
    document.title = 'rp1-urdf-error:' + message;
  }
  function zoom(factor) {
    if (!ready) return;
    const offset = camera.position.clone().sub(controls.target);
    offset.setLength(THREE.MathUtils.clamp(offset.length() * factor, controls.minDistance, controls.maxDistance));
    camera.position.copy(controls.target).add(offset); controls.update();
  }
  el('pause').addEventListener('click', () => { paused = !paused; setPauseLabel(); clock(); });
  el('reset').addEventListener('click', () => {
    if (homePosition) { camera.position.copy(homePosition); controls.target.copy(homeTarget); controls.update(); }
  });
  el('zoom-in').addEventListener('click', () => zoom(.8));
  el('zoom-out').addEventListener('click', () => zoom(1.25));
  document.addEventListener('visibilitychange', () => { clock(); startFrames(); });
  window.addEventListener('pagehide', shutdown);
  const observer = new ResizeObserver(resize);
  observer.observe(host);
  window.rp1ManualGuide = { configure, shutdown, status: () => ({ ready, closed, selected: selected?.joint.name || null, paused }) };

  async function load() {
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.6));
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = .95;
      renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
      renderer.domElement.setAttribute('aria-label', 'RP1 URDF 模型动画，非实测姿态');
      renderer.domElement.tabIndex = 0;
      renderer.domElement.addEventListener('webglcontextlost', event => { event.preventDefault(); fail('WebGL 上下文已丢失'); });
      host.appendChild(renderer.domElement);
      scene = new THREE.Scene();
      camera = new THREE.PerspectiveCamera(34, 1, .01, 100);
      controls = new OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true; controls.dampingFactor = .08;
      controls.screenSpacePanning = true;
      controls.addEventListener('start', () => { interaction = true; clock(); });
      controls.addEventListener('end', () => { interaction = false; clock(); });
      const pmrem = new THREE.PMREMGenerator(renderer);
      const room = new RoomEnvironment();
      environment = pmrem.fromScene(room, .04); scene.environment = environment.texture;
      room.dispose(); pmrem.dispose();
      scene.add(new THREE.HemisphereLight(0xe7f1ff, 0xa2afbd, 1.5));
      const key = new THREE.DirectionalLight(0xfffaf1, 2.8); key.position.set(3, 4.5, 3); key.castShadow = true;
      key.shadow.mapSize.set(1024, 1024); key.shadow.normalBias = .015;
      key.shadow.camera.left = -2; key.shadow.camera.right = 2; key.shadow.camera.top = 2; key.shadow.camera.bottom = -2;
      scene.add(key);
      const rim = new THREE.DirectionalLight(0xbed8ff, 1.5); rim.position.set(-3, 2, -2); scene.add(rim);
      const response = await fetch('/models/rp1-urdf-guide.json');
      if (!response.ok) throw new Error('缺少 URDF 元数据');
      metadata = await response.json();
      if (metadata.source_kind !== 'urdf_model' || !metadata.joints?.length) throw new Error('模型来源无效');
      const loader = new GLTFLoader(); loader.setMeshoptDecoder(MeshoptDecoder);
      const gltf = await loader.loadAsync('/models/rp1-urdf-guide.glb');
      if (closed) return;
      rig = gltf.scene;
      rig.traverse(item => {
        if (item.userData.urdfJoint) {
          const name = item.userData.urdfJoint;
          if (nodes.has(name)) throw new Error('重复 URDF 关节');
          nodes.set(name, item); bases.set(name, item.quaternion.toArray());
        }
        if (item.isMesh) {
          const visual = item.name.toLowerCase();
          const material = visual.includes('battery') ? dark : visual.includes('head') ? brand : grey;
          item.material = material; originalMaterials.set(item, material);
          item.castShadow = item.receiveShadow = true;
        }
      });
      for (const row of metadata.joints) if (!nodes.has(row.name)) throw new Error('模型缺少关节 ' + row.name);
      root.add(rig); scene.add(root);
      pose = baselinePose(metadata, null); resetPose();
      const bounds = new THREE.Box3().setFromObject(root);
      const ground = new THREE.Mesh(new THREE.PlaneGeometry(12, 12), new THREE.ShadowMaterial({ color: 0x455d7b, opacity: .2 }));
      ground.rotation.x = -Math.PI / 2; ground.position.y = bounds.min.y - .003; ground.receiveShadow = true; scene.add(ground);
      marker = new THREE.Mesh(new THREE.SphereGeometry(.018, 20, 12),
        new THREE.MeshBasicMaterial({ color: 0x3285db, transparent: true, opacity: .85, depthTest: false }));
      marker.renderOrder = 5; marker.visible = false; scene.add(marker);
      ready = true;
      resize(); configure(pending); setPauseLabel();
      if (closed || !ready) return;
      el('loading').hidden = true;
      for (const button of el('toolbar').querySelectorAll('button')) button.disabled = false;
      document.title = 'rp1-urdf-ready';
      startFrames();
    } catch (error) { fail(error.message || 'WebGL 加载失败'); }
  }
  load();
}
if (typeof document !== 'undefined') mount();
