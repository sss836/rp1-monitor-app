/* Reuse one live heat map in the overview subview and the full temperature page. */
(() => {
  const el = id => document.getElementById(id);
  const structure = el('overview-structure-panel'), thermal = el('overview-thermal-panel');
  const host = el('overview-thermal-host'), heatMap = document.querySelector('[data-page-panel="temperature"] .motor-map-panel');
  if (!structure || !thermal || !host || !heatMap) return;
  const home = heatMap.parentElement, sibling = heatMap.nextSibling;
  const tabs = {structure:el('overview-structure-tab'),thermal:el('overview-thermal-tab')};
  let view = 'structure', lastActive = null;
  function update() {
    const active = Boolean(window.rp1Pages?.isActive('overview'));
    const showing = active && view === 'thermal';
    let changed = active !== lastActive || structure.hidden !== (view !== 'structure') || thermal.hidden !== (view !== 'thermal');
    lastActive = active;
    if (showing && heatMap.parentElement !== host) { host.append(heatMap); changed = true; }
    else if (!showing && heatMap.parentElement !== home) { home.insertBefore(heatMap,sibling); changed = true; }
    structure.hidden = view !== 'structure'; thermal.hidden = view !== 'thermal';
    for (const [key,tab] of Object.entries(tabs)) {
      tab.setAttribute('aria-selected',String(view === key)); tab.tabIndex = view === key ? 0 : -1;
    }
    if (changed) document.dispatchEvent(new CustomEvent('rp1:overview-view',{detail:{view}}));
  }
  function select(key, focus=false) {
    view = key; update();
    if (focus) tabs[key].focus({preventScroll:true});
  }
  for (const [key,tab] of Object.entries(tabs)) {
    tab.addEventListener('click',()=>select(key));
    tab.addEventListener('keydown',event=>{
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      select(event.key === 'Home' ? 'structure' : event.key === 'End' ? 'thermal' : key === 'structure' ? 'thermal' : 'structure',true);
    });
  }
  document.addEventListener('rp1:page',update);
  window.rp1OverviewViews = {refresh:update};
  update();
})();
