(() => {
  'use strict';
  const mock=JSON.parse(document.getElementById('mock-data').textContent);
  const select=document.getElementById('screens');
  mock.screens.forEach(s=>{const option=document.createElement('option');option.value=s.id;option.textContent=s.title;select.append(option);});
  const draw=()=>window.StudioMock.render(document.getElementById('canvas'),mock,select.value,{onNavigate:id=>{select.value=id;draw();}});
  select.addEventListener('change',draw);draw();
})();
