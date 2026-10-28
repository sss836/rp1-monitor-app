import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';
import Swiper from 'swiper';
import { A11y, EffectCoverflow, Navigation } from 'swiper/modules';

// All assets and decoders are bundled locally; no robot control or external service.
const byId = id => document.getElementById(id);
const stage = byId('model-stage');
const host = byId('model-canvas-host');
const modelPanel = stage?.closest('[data-page-panel]');
const structurePanel = byId('overview-structure-panel');
const status = byId('model-status');
const progress = byId('model-progress');
const retry = byId('model-retry');
const staticButton = byId('model-static');
const fallback = byId('model-fallback');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
let paused = document.body.dataset.motion === 'paused';
let reduceMotion = reduced.matches;
let hovering = false, focused = false, visible = true, staticMode = false;
let renderer, scene, camera, controls, model, environment;
let frameId = 0, lastFrame = 0, introElapsed = 0, generation = 0, lastAspect = 1;
let ready = false, failed = false, introStopped = false;
let homePosition, homeTarget;
const INTRO_SECONDS = 8;
const modelControls = ['model-reset', 'model-left', 'model-right', 'model-zoom-in', 'model-zoom-out'];
const modelURL = new URL('./models/rp1-static-structure.glb', new URL('../', import.meta.url));

function setStatus(message) { if (status) status.textContent = message; }
function syncModelVisibility() {
  const showModel = ready && !failed && !staticMode;
  if (host) host.hidden = !showModel;
  if (fallback) fallback.hidden = showModel;
}
function modelPageActive() { return (!modelPanel || !modelPanel.hidden) && (!structurePanel || !structurePanel.hidden); }
function canRender() { return ready && !failed && !staticMode && modelPageActive() && visible && !document.hidden; }
function automaticMotion() { return canRender() && !paused && !reduceMotion && !hovering && !focused && !introStopped && introElapsed < INTRO_SECONDS; }
function cancelFrame() { if (frameId) cancelAnimationFrame(frameId); frameId = 0; lastFrame = 0; }
function renderFrame(time) {
  frameId = 0;
  if (!canRender()) { lastFrame = 0; return; }
  if (automaticMotion()) {
    // Count only visible, unpaused seconds. The presentation rotates left once.
    if (lastFrame) introElapsed = Math.min(INTRO_SECONDS, introElapsed + Math.min((time - lastFrame) / 1000, 0.08));
    model.rotation.y = 0.32 - 0.64 * (introElapsed / INTRO_SECONDS);
    lastFrame = time;
  } else lastFrame = 0;
  controls.update();
  renderer.render(scene, camera);
  if (automaticMotion()) frameId = requestAnimationFrame(renderFrame);
}
function requestRender() { if (!frameId && canRender()) frameId = requestAnimationFrame(renderFrame); }
function updateMotion() { cancelFrame(); requestRender(); }
function setControlsEnabled(enabled) {
  for (const id of modelControls) if (byId(id)) byId(id).disabled = !enabled;
}
function disposeModel(object) {
  if (!object) return;
  const geometries = new Set(), materials = new Set(), textures = new Set();
  object.traverse(item => {
    if (item.geometry) geometries.add(item.geometry);
    for (const material of Array.isArray(item.material) ? item.material : [item.material]) if (material) {
      materials.add(material);
      for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
    }
  });
  for (const value of [...geometries, ...materials, ...textures]) value.dispose();
}
function teardown() {
  cancelFrame(); ready = false;
  controls?.dispose(); controls = null;
  disposeModel(scene); scene = null; model = null;
  environment?.dispose(); environment = null;
  renderer?.dispose(); renderer?.domElement.remove(); renderer = null;
}
function showFailure(message) {
  failed = true;
  teardown();
  stage?.classList.remove('model-ready');
  stage?.classList.add('model-unavailable');
  host?.setAttribute('aria-hidden', 'true');
  if (retry) retry.hidden = false;
  if (progress) progress.hidden = true;
  setControlsEnabled(false);
  if (staticButton) staticButton.disabled = true;
  syncModelVisibility();
  setStatus(message + ' · 已显示静态外观图。');
}
function resize() {
  if (!renderer || !camera || !host) return;
  const width = host.clientWidth || stage?.clientWidth || 0, height = host.clientHeight || stage?.clientHeight || 0;
  // A hidden page has no layout. Keep the last valid projection until it returns.
  if (!modelPageActive() || width <= 0 || height <= 0) return;
  lastAspect = width / height;
  renderer.setSize(width, height, false);
  camera.aspect = lastAspect; camera.updateProjectionMatrix();
  requestRender();
}
function stopIntro() { introStopped = true; cancelFrame(); requestRender(); }
function rotate(direction) {
  if (!ready || staticMode) return;
  stopIntro();
  const offset = camera.position.clone().sub(controls.target);
  offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), direction * Math.PI / 12);
  camera.position.copy(controls.target).add(offset); controls.update(); requestRender();
}
function zoom(factor) {
  if (!ready || staticMode) return;
  stopIntro();
  const offset = camera.position.clone().sub(controls.target);
  offset.setLength(THREE.MathUtils.clamp(offset.length() * factor, controls.minDistance, controls.maxDistance));
  camera.position.copy(controls.target).add(offset); controls.update(); requestRender();
}
function reset() {
  if (!ready) return;
  introStopped = true; model.rotation.y = 0;
  camera.position.copy(homePosition); controls.target.copy(homeTarget); controls.update(); requestRender();
  setStatus('已恢复正面视角 · 拖动旋转，滚轮或双指缩放。');
}
function setStatic(value) {
  staticMode = value;
  stage?.classList.toggle('model-static', value);
  stage?.classList.toggle('model-ready', ready && !value);
  host?.setAttribute('aria-hidden', String(value));
  if (renderer) renderer.domElement.tabIndex = value ? -1 : 0;
  if (staticButton) { staticButton.setAttribute('aria-pressed', String(value)); staticButton.textContent = value ? '查看三维模型' : '静态展示'; }
  setControlsEnabled(ready && !value);
  syncModelVisibility();
  resize();
  setStatus(value ? '静态外观图 · 非实时姿态。' : 'RP1.3 结构模型 · 拖动旋转，滚轮或双指缩放。');
  updateMotion();
}
async function loadModel() {
  if (!stage || !host) return;
  const current = ++generation;
  teardown(); failed = false; introElapsed = 0; introStopped = false;
  syncModelVisibility();
  stage.classList.remove('model-ready', 'model-unavailable');
  if (retry) retry.hidden = true;
  if (progress) { progress.hidden = false; progress.removeAttribute('value'); }
  setControlsEnabled(false);
  if (staticButton) staticButton.disabled = true;
  setStatus('正在加载 RP1.3 结构模型…');
  try {
    renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: 'low-power' });
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.5));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 0.85;
    const canvas = renderer.domElement;
    canvas.className = 'model-webgl-canvas'; canvas.tabIndex = 0;
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', 'RP1.3 静态三维结构，非实时姿态。方向键旋转，加减键缩放，Home恢复正面。');
    canvas.addEventListener('webglcontextlost', event => {
      event.preventDefault();
      if (current === generation) { generation++; showFailure('三维渲染已中断，请重试'); }
    });
    canvas.addEventListener('keydown', event => {
      if (['ArrowLeft', 'ArrowRight', '+', '=', '-', '_', 'Home'].includes(event.key)) {
        event.preventDefault();
        if (event.key === 'ArrowLeft') rotate(-1);
        else if (event.key === 'ArrowRight') rotate(1);
        else if (event.key === 'Home') reset();
        else zoom(['+', '='].includes(event.key) ? 0.88 : 1.12);
      }
    });
    host.replaceChildren(canvas);
    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(29, 1, 0.02, 100);
    scene.add(new THREE.HemisphereLight(0xffffff, 0xaab8ce, 0.7));
    const key = new THREE.DirectionalLight(0xffffff, 2.0); key.position.set(4, 5, 4); scene.add(key);
    const fill = new THREE.DirectionalLight(0xd8e7ff, 0.6); fill.position.set(-4, 2, 2); scene.add(fill);
    const room = new RoomEnvironment();
    const pmrem = new THREE.PMREMGenerator(renderer);
    environment = pmrem.fromScene(room, 0.04); scene.environment = environment.texture;
    scene.environmentIntensity = 0.65;
    room.dispose(); pmrem.dispose();
    controls = new OrbitControls(camera, canvas);
    // OrbitControls sets an inline "none" value; retain native vertical page scrolling.
    canvas.style.touchAction = 'pan-y';
    // Vertical touch gestures scroll the page; horizontal drag rotates, two fingers zoom.
    canvas.style.touchAction = 'pan-y';
    controls.enablePan = false; controls.enableDamping = false;
    controls.minPolarAngle = Math.PI * 0.16; controls.maxPolarAngle = Math.PI * 0.82;
    controls.addEventListener('start', stopIntro);
    controls.addEventListener('change', requestRender);
    resize();
    const loader = new GLTFLoader().setMeshoptDecoder(MeshoptDecoder);
    const gltf = await loader.loadAsync(modelURL.href, event => {
      if (current !== generation || !progress) return;
      if (event.lengthComputable && event.total > 0) {
        progress.max = event.total; progress.value = event.loaded;
        setStatus(`正在加载 RP1.3 结构模型 · ${Math.min(100, Math.round(event.loaded / event.total * 100))}%`);
      }
    });
    if (current !== generation) { disposeModel(gltf.scene); return; }
    model = gltf.scene;
    const box = new THREE.Box3().setFromObject(model);
    const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
    if (!(size.y > 0 && Number.isFinite(size.y))) throw new Error('empty model');
    const scale = 2.8 / size.y;
    model.position.copy(center).multiplyScalar(-scale); model.scale.setScalar(scale);
    // Center in an independent group so the introductory orbit does not move the CAD origin.
    const pivot = new THREE.Group(); pivot.add(model); scene.add(pivot); model = pivot;
    model.traverse(item => { if (item.isMesh) item.frustumCulled = true; });
    // A static soft contact shadow keeps an on-demand render inexpensive on host PCs.
    const shadowCanvas = document.createElement('canvas'); shadowCanvas.width = shadowCanvas.height = 128;
    const shadowContext = shadowCanvas.getContext('2d');
    const shadowGradient = shadowContext.createRadialGradient(64, 64, 2, 64, 64, 63);
    shadowGradient.addColorStop(0, 'rgba(55,85,130,0.24)');
    shadowGradient.addColorStop(0.45, 'rgba(70,99,145,0.12)');
    shadowGradient.addColorStop(1, 'rgba(70,99,145,0)');
    shadowContext.fillStyle = shadowGradient; shadowContext.fillRect(0, 0, 128, 128);
    const shadowTexture = new THREE.CanvasTexture(shadowCanvas);
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(2.4, 1.5), new THREE.MeshBasicMaterial({ map: shadowTexture, transparent: true, depthWrite: false }));
    floor.rotation.x = -Math.PI / 2; floor.position.y = -1.405; scene.add(floor);
    const aspect = Math.max(lastAspect, 0.4);
    const distance = Math.max(3.15 / (2 * Math.tan(THREE.MathUtils.degToRad(29 / 2))), size.x * scale / aspect / (2 * Math.tan(THREE.MathUtils.degToRad(29 / 2))) * 1.15);
    homePosition = new THREE.Vector3(0.18, 0.03, distance); homeTarget = new THREE.Vector3(0, -0.02, 0);
    camera.position.copy(homePosition); controls.target.copy(homeTarget);
    controls.minDistance = distance * 0.45; controls.maxDistance = distance * 1.8; controls.update();
    ready = true;
    syncModelVisibility();
    if (progress) progress.hidden = true;
    if (staticButton) staticButton.disabled = false;
    stage.classList.toggle('model-ready', !staticMode);
    host.setAttribute('aria-hidden', String(staticMode));
    setControlsEnabled(!staticMode);
    setStatus('RP1.3 结构模型 · 拖动旋转，滚轮或双指缩放。');
    resize(); requestRender();
  } catch (error) {
    if (current === generation) showFailure('三维模型暂不可用，请重试');
  }
}

if (stage && host) {
  const motionBoundary = stage.closest('.model-exhibit') || stage;
  motionBoundary.addEventListener('pointerenter', () => { hovering = true; updateMotion(); });
  motionBoundary.addEventListener('pointerleave', () => { hovering = false; updateMotion(); });
  const focusRegion = stage.closest('.model-exhibit') || stage;
  focusRegion.addEventListener('focusin', () => { focused = true; updateMotion(); });
  focusRegion.addEventListener('focusout', event => { focused = focusRegion.contains(event.relatedTarget); updateMotion(); });
  retry?.addEventListener('click', loadModel);
  byId('model-reset')?.addEventListener('click', reset);
  byId('model-left')?.addEventListener('click', () => rotate(-1));
  byId('model-right')?.addEventListener('click', () => rotate(1));
  byId('model-zoom-in')?.addEventListener('click', () => zoom(0.88));
  byId('model-zoom-out')?.addEventListener('click', () => zoom(1.12));
  staticButton?.addEventListener('click', () => setStatic(!staticMode));
  new ResizeObserver(resize).observe(host);
  new IntersectionObserver(entries => { visible = entries[0].isIntersecting; updateMotion(); }, { rootMargin: '0px' }).observe(stage);
  // Loading on first entry avoids initializing WebGL for an unseen overview.
  if (modelPageActive()) loadModel();
}

let carousel;
let syncingFleet = false;
let fleetSlideNodes = [];
let fleetSelectedId;
function currentRobot() { return byId('fleet-coverflow')?.querySelector('.fleet-card[aria-pressed="true"]')?.closest('[data-robot-id]')?.dataset.robotId; }
function updateFleet(selectedId = currentRobot(), forceLayout = false) {
  const element = byId('fleet-coverflow');
  if (!element || element.closest('[data-page-panel]')?.hidden) return;
  const slides = [...element.querySelectorAll('.swiper-slide')];
  const slidesChanged = slides.length !== fleetSlideNodes.length || slides.some((slide, index) => slide !== fleetSlideNodes[index]);
  const selectionChanged = selectedId !== fleetSelectedId;
  let initialized = false;
  syncingFleet = true;
  if (!carousel && slides.length) {
    carousel = new Swiper(element, {
      modules: [A11y, EffectCoverflow, Navigation],
      effect: 'coverflow', grabCursor: true, centeredSlides: true,
      slidesPerView: 'auto', spaceBetween: 24, speed: reduceMotion ? 0 : 800,
      loop: false, watchOverflow: true,
      coverflowEffect: { rotate: reduceMotion ? 0 : 14, stretch: 0, depth: reduceMotion ? 0 : 100, modifier: 1, slideShadows: false, scale: reduceMotion ? 1 : 0.94 },
      navigation: { prevEl: '#fleet-prev', nextEl: '#fleet-next' },
      a11y: { prevSlideMessage: '上一台机器人', nextSlideMessage: '下一台机器人', slideLabelMessage: '机器人 {{index}} / {{slidesLength}}', containerRoleDescriptionMessage: '机器人选择轮播', itemRoleDescriptionMessage: '机器人' },
      on: { slideChange(swiper) {
        const robotId = swiper.slides[swiper.activeIndex]?.dataset.robotId;
        positionFleet(swiper);
        if (!syncingFleet && robotId) window.rp1SelectRobot?.(robotId);
      } },
    });
    initialized = true;
    // Scope arrow keys to the fleet itself so model controls and form selects
    // cannot accidentally change the monitored robot.
    element.addEventListener('keydown', event => {
      if (event.defaultPrevented || event.target.closest('input, select, textarea, [contenteditable="true"]')) return;
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        event.key === 'ArrowLeft' ? carousel.slidePrev() : carousel.slideNext();
      }
    });
  }
  if (!carousel) { syncingFleet = false; return; }
  // A telemetry poll changes card values, not Swiper geometry. Updating every
  // second would reset translate/transition and interrupt user drags.
  if ((slidesChanged || forceLayout) && !initialized) carousel.update();
  fleetSlideNodes = slides;
  fleetSelectedId = selectedId;
  const index = slides.findIndex(slide => slide.dataset.robotId === selectedId);
  if ((initialized || slidesChanged || selectionChanged || forceLayout) && index >= 0 && carousel.activeIndex !== index) carousel.slideTo(index, forceLayout || reduceMotion ? 0 : 800, false);
  positionFleet(carousel);
  syncingFleet = false;
}
function positionFleet(swiper) {
  const position = byId('fleet-position');
  if (position) position.textContent = `${swiper.slides.length ? swiper.activeIndex + 1 : 0} / ${swiper.slides.length}`;
  for (const [id, atBoundary] of [['fleet-prev', swiper.isBeginning], ['fleet-next', swiper.isEnd]]) {
    const button = byId(id); if (button) button.disabled = atBoundary || swiper.slides.length < 2;
  }
}
function pageChanged() {
  // Stop synchronously on page departure; IntersectionObserver can arrive later.
  cancelFrame();
  if (modelPageActive() && stage && host) {
    const bounds = stage.getBoundingClientRect();
    visible = bounds.width > 0 && bounds.height > 0 && bounds.bottom > 0 && bounds.top < innerHeight && bounds.right > 0 && bounds.left < innerWidth;
    hovering = stage.closest('.model-exhibit')?.matches(':hover') || false;
    focused = (stage.closest('.model-exhibit') || stage).contains(document.activeElement);
    if (!renderer && !failed) loadModel();
    else { resize(); requestRender(); }
  } else {
    visible = false; hovering = false; focused = false;
  }
  updateFleet(currentRobot(), true);
}
function motionChanged(event) {
  paused = event?.detail?.paused ?? document.body.dataset.motion === 'paused';
  const nextReduceMotion = event?.detail?.reduced ?? reduced.matches;
  const reduceMotionChanged = nextReduceMotion !== reduceMotion;
  reduceMotion = nextReduceMotion;
  if (carousel && reduceMotionChanged) {
    carousel.params.speed = reduceMotion ? 0 : 800;
    Object.assign(carousel.params.coverflowEffect, { rotate: reduceMotion ? 0 : 14, depth: reduceMotion ? 0 : 100, scale: reduceMotion ? 1 : 0.94 });
    carousel.update();
  }
  updateMotion();
}
document.addEventListener('rp1:page', pageChanged);
document.addEventListener('rp1:overview-view', pageChanged);
document.addEventListener('rp1:motion', motionChanged);
reduced.addEventListener('change', () => motionChanged());
document.addEventListener('visibilitychange', updateMotion);
document.addEventListener('rp1:fleet-ready', event => updateFleet(event.detail?.selectedId));
window.rp1Showcase = { updateFleet };
updateFleet();
