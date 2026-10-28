/* Instrument-style rendering of mapped joint readings, never camera pixels. */
(() => {
  const el = id => document.getElementById(id), svg = el('motor-map');
  const panel = svg.closest('.motor-map-panel'), ns = 'http://www.w3.org/2000/svg';
  const palette = ['#3042b0', '#7842bb', '#c23c86', '#ef5a44', '#ff9a39', '#ffe15a', '#fff4ba'];
  let mode = 'infrared', snapshot = null;
  const color = (value, low, high) => {
    const position = Math.max(0, Math.min(1, (value-low)/(high-low))) * (palette.length-1);
    const index = Math.min(palette.length-2, Math.floor(position)), ratio = position-index;
    const channels = [1,3,5].map(offset => Math.round(parseInt(palette[index].slice(offset,offset+2),16)*(1-ratio)+parseInt(palette[index+1].slice(offset,offset+2),16)*ratio));
    return '#' + channels.map(channel => channel.toString(16).padStart(2,'0')).join('');
  };
  function node(tag, attributes) {
    const item = document.createElementNS(ns, tag);
    for (const [key,value] of Object.entries(attributes)) item.setAttribute(key, String(value));
    return item;
  }
  function enhance(view) {
    snapshot = view;
    const jointView = !!window.rp1JointMap;
    const readings = (jointView ? view?.rows || [] : view?.regions || []).filter(region => Number.isFinite(region.value) && ['hot','below'].includes(region.state));
    const low = Number.isFinite(view?.scale_min_c) ? view.scale_min_c : 20;
    const high = Number.isFinite(view?.scale_max_c) && view.scale_max_c > low ? view.scale_max_c : low+80;
    el('thermal-image-range').textContent = readings.length ? `${jointView ? '关节读数' : '映射测点'} ${Math.min(...readings.map(r=>r.value)).toFixed(1)} — ${Math.max(...readings.map(r=>r.value)).toFixed(1)} °C` : '关节测点温度未知';
    panel.style.setProperty('--thermal-gradient', mode === 'infrared' ? `linear-gradient(90deg,${palette.join(',')})` : 'linear-gradient(90deg,#fff5f0,#ffcfb8,#ef6b6e,#8e1235)');
    let meaning = view?.meaning || '当前没有有效测温数据。';
    if (jointView) meaning = meaning.replace('同一部位显示已知最高温度，逐电机数据见列表。','各关节独立示温；图上 J 编号对应关节索引，同部位多轴位置为示意布局。');
    el('motor-map-meaning').textContent = mode === 'infrared' ? meaning.replace('颜色越深温度越高', '色谱由蓝紫至橙黄表示温度升高') : meaning;
    if (mode !== 'infrared') return;
    const group = el('motor-map-regions'), defs = node('defs', {});
    for (const regionNode of group.querySelectorAll('g[data-region]')) {
      const jointPoint = regionNode.dataset.joint !== undefined;
      const region = jointPoint ? (view?.rows || []).find(item => String(item.joint) === regionNode.dataset.joint) : (view?.regions || []).find(item => item.key === regionNode.dataset.region);
      const ellipse = regionNode.querySelector('ellipse'), text = regionNode.querySelector('text');
      if (!region || !ellipse) continue;
      const known = Number.isFinite(region.value) && ['hot','below'].includes(region.state);
      if (known) {
        const fill = color(region.value,low,high), id = `joint-heat-${defs.children.length}`;
        const gradient = node('radialGradient', {id});
        for (const [offset,opacity] of [['0%',1],['34%',.98],['65%',.6],['100%',0]]) gradient.append(node('stop', {offset,'stop-color':fill,'stop-opacity':opacity}));
        defs.append(gradient);
        ellipse.setAttribute('fill', `url(#${id})`);
        ellipse.setAttribute('rx', jointPoint ? 73 : 115); ellipse.setAttribute('ry', jointPoint ? 68 : 100);
        ellipse.setAttribute('fill-opacity', 1); ellipse.setAttribute('stroke', 'none');
        const marker = node('ellipse', {cx:ellipse.getAttribute('cx'),cy:ellipse.getAttribute('cy'),rx:jointPoint ? 36 : 47,ry:jointPoint ? 31 : 37,fill,'fill-opacity':.96,stroke:region.state === 'hot' ? '#fff4ba' : '#e5eafa','stroke-opacity':region.state === 'hot' ? 1 : .45,'stroke-width':region.state === 'hot' ? 5 : 2});
        regionNode.insertBefore(marker, text);
      } else {
        ellipse.setAttribute('fill', '#667184'); ellipse.setAttribute('fill-opacity', .85);
        ellipse.setAttribute('stroke', '#b3bdcc'); ellipse.setAttribute('stroke-width', 2);
      }
      if (text) {
        text.setAttribute('fill', '#ffffff'); text.setAttribute('stroke', '#111d30');
        text.setAttribute('stroke-width', 6); text.setAttribute('paint-order', 'stroke');
        text.setAttribute('font-size', jointPoint ? 29 : 41);
      }
    }
    if (defs.children.length) group.prepend(defs);
    for (const rowNode of el('motor-map-rows').querySelectorAll('li[data-joint]')) {
      const row = (view?.rows || []).find(item => String(item.joint) === rowNode.dataset.joint);
      const swatch = rowNode.querySelector('.motor-temperature-swatch');
      if (swatch && row && Number.isFinite(row.value) && ['hot','below'].includes(row.state)) swatch.style.backgroundColor = color(row.value,low,high);
    }
    if (jointView) for (const button of el('unmapped-joint-list').querySelectorAll('button[data-joint]')) {
      const row = (view?.rows || []).find(item => String(item.joint) === button.dataset.joint);
      const fill = row && Number.isFinite(row.value) && ['hot','below'].includes(row.state) ? color(row.value,low,high) : '#667184';
      button.querySelector('.unmapped-joint-swatch')?.style.setProperty('--joint-color', fill);
    }
  }
  function setMode(value) {
    if (!['infrared','red'].includes(value)) return;
    mode = value; svg.dataset.thermalMode = value; panel.dataset.thermalMode = value;
    for (const kind of ['infrared','red']) el(`thermal-mode-${kind}`).setAttribute('aria-pressed', String(kind === value));
    renderMotorMap(snapshot);
  }
  window.rp1TemperatureView = {setMode, enhance};
  for (const kind of ['infrared','red']) el(`thermal-mode-${kind}`).addEventListener('click', () => setMode(kind));
})();
