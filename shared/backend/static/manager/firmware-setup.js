/* Product preparation center: preserve existing navigation and configuration. */
(() => {
  let timer, busy = false, active = null, probe = null, generation = 0, flashed = null;
  const names = {host:'主机',fonts:'面板字体',tools:'构建工具',storage:'可写空间',configuration:'当前配置',wifi:'Wi-Fi',cloud:'云端凭据',target:'硬件目标',network:'网络依赖',usb:'USB 与设备'};
  async function load(){return loadFirmware()}
  firmwareController.subscribe(state=>{
    if(!state.data){
      active=null;
      $('preparationChecks').innerHTML='<p>'+html(state.error?'准备检查失败，请重试':'正在检查已应用配置与准备条件…')+'</p>';
      $('preparationJob').textContent=state.error||'正在检查，不使用上次成功状态';
      $('importFonts').disabled=true;$('prepareTools').disabled=true;
      $('cancelPreparation').hidden=true;
      return;
    }
    const setup=state.data.preparation, flight=state.data.preflight, history=state.data.history;
    const checks={...flight.checks,...Object.fromEntries(['host','fonts','tools','storage'].map(key=>[key,setup[key]]))};
    $('preparationChecks').innerHTML=Object.entries(checks).map(([key,row])=>`<div><strong>${html(names[key]||key)}</strong><span class="pill ${row.ready?'good':row.required_for_build===false?'':'warn'}">${row.ready?'已就绪':row.state==='on_demand'?'按需联网':row.state==='physical_action'?'需连接设备':row.state==='unsupported'?'不支持':row.state==='not_installed'?'尚未准备':'待处理'}</span><p>${html(row.detail)}</p></div>`).join('');
    $('preparationChecks').setAttribute('aria-busy',String(state.checking));
    active=state.preparing;
    const latest=active||(setup.jobs||[])[0];
    $('preparationJob').textContent=state.checking?'正在检查最新准备状态':latest?`${latest.kind==='fonts'?'字体':'工具'}：${firmwareStatus(latest.status,latest.kind==='fonts'?'导入字体':'准备工具')}。 ${latest.detail||''}`:'尚无准备任务。不会自动下载工具或字体。';
    const locked=busy||state.checking||!!active||state.writing||['queued','running'].includes(state.build?.status);
    $('importFonts').disabled=locked||!setup.host.ready;
    $('prepareTools').disabled=locked||!setup.host.ready||setup.tools.state==='unsupported_runtime';
    $('cancelPreparation').hidden=!active;
    $('cancelPreparation').disabled=busy;
    flashed=(history.flashes||[])[0]||null;
    const built=state.build,evidence=[];
    if(built)evidence.push(['固件构建',built.stale?'配置或源码已变化，请重新构建':built.status==='completed'?`已完成，SHA256 ${built.artifacts['firmware.bin'].sha256.slice(0,16)}`:built.error||firmwareStatus(built.status)]);
    if(flashed){
      evidence.push(['写入校验',flashed.verified_at?'已完成':flashed.progress||flashed.error||firmwareStatus(flashed.status,'写入')]);
      evidence.push(['设备联网',flashed.network_verified?'已取得匹配设备回执':'尚未确认，不以后台健康代替']);
      evidence.push(['实体屏',flashed.physical_display_confirmed?'用户已人工确认':'待用户观察确认']);
      if(flashed.install_mode==='update')evidence.push(['启动后设置',flashed.protected_settings_verified?'已核对受保护设置':flashed.status==='settings_changed'?flashed.error:'尚未核对，不能仅凭联网推定设置未变']);
      if(flashed.error)evidence.push(['需检查',flashed.error]);
    }
    $('firmwareEvidence').innerHTML=evidence.map(([label,value])=>`<div><dt>${html(label)}</dt><dd>${html(value)}</dd></div>`).join('');
    $('confirmPhysical').hidden=!flashed?.verified_at||flashed.physical_display_confirmed;
  });
  async function execute(button,action) {
    if(busy)return;busy=true;button.disabled=true;
    try {await action();await load()}
    catch(error){$('preparationJob').textContent=error.message;toast(error.message,true)}
    finally {busy=false;button.disabled=false;await load()}
  }
  $('fontImportForm').addEventListener('submit',event=>{
    event.preventDefault();if(!$('fontPermission').checked)return;
    execute($('importFonts'),async()=>{
      const parts=[];
      for(const [role,id] of [['regular','regularFont'],['bold','boldFont']]){
        const file=$(id).files[0];if(!file||file.size>32*1024*1024)throw new Error('请选择两份不超过32MiB的字体');
        parts.push(file);
      }
      const types=parts.map(file=>'.'+file.name.split('.').pop().toLowerCase());
      if(types.some(type=>!['.otf','.ttf'].includes(type)))throw new Error('请选择 OTF 或 TTF 字体');
      const response=await fetch('/api/local-console/firmware/preparation/fonts',{method:'POST',credentials:'same-origin',
        headers:{'X-CSRF-Token':csrf,'Content-Type':'application/octet-stream',
          'X-InkSight-Regular-Bytes':String(parts[0].size),'X-InkSight-Regular-Type':types[0],'X-InkSight-Bold-Type':types[1]},
        body:new Blob(parts,{type:'application/octet-stream'})});
      const result=await response.json();if(!response.ok)throw new Error(result.detail||result.error||'字体上传失败');
      $('regularFont').value='';$('boldFont').value='';$('fontPermission').checked=false;
    });
  });
  $('prepareTools').addEventListener('click',()=>{
    if(!confirm('从 PyPI / PlatformIO 官方注册源下载固定版本工具到私有目录？可能下载较大文件；不会修改系统环境或写入设备。'))return;
    execute($('prepareTools'),()=>api('/firmware/preparation/tools',{method:'POST'}));
  });
  $('cancelPreparation').addEventListener('click',()=>{if(active)execute($('cancelPreparation'),()=>api(`/firmware/preparation/${encodeURIComponent(active.id)}/cancel`,{method:'POST'}))});
  $('checkPreparation').addEventListener('click',load);
  $('confirmPhysical').addEventListener('click',()=>execute($('confirmPhysical'),async()=>{
    if(!flashed||!confirm(`确认你已观察指纹 ${flashed.device_fingerprint} 的实体屏，画面与本次更新预期相符？这只记录人工验收，不代表联网校验。`))return;
    await api(`/firmware/flashes/${encodeURIComponent(flashed.id)}/display-confirmation`,{method:'POST',body:JSON.stringify({confirmed:true})});
  }));
  $('detectPreparationUsb').addEventListener('click',()=>execute($('detectPreparationUsb'),async()=>{
    const data=await api('/firmware/devices');probe=null;$('bindPreparationUsb').hidden=true;
    $('preparationUsb').innerHTML=data.devices.length?data.devices.map(row=>`<option value="${html(row.id)}">${html(row.label)} / ${html(row.path_hint)}</option>`).join(''):'<option value="">未发现设备，请检查数据线与USB接口</option>';
    $('probePreparationUsb').disabled=!data.devices.length;
  }));
  $('probePreparationUsb').addEventListener('click',()=>execute($('probePreparationUsb'),async()=>{
    if(!confirm('只读识别所选 USB 板卡？过程会短暂重启设备，不会写入 Flash。'))return;
    const data=await api('/firmware/probe',{method:'POST',body:JSON.stringify({device_id:$('preparationUsb').value})});probe=data.probe;
    $('usbIdentity').textContent=`${probe.chip} / ${probe.flash_size}，设备指纹 ${probe.device_fingerprint}。请核对实物为支持板型再绑定。`;
    $('bindPreparationUsb').hidden=false;
  }));
  $('preparationUsb').addEventListener('change',()=>{probe=null;$('bindPreparationUsb').hidden=true});
  $('bindPreparationUsb').addEventListener('click',()=>execute($('bindPreparationUsb'),async()=>{
    if(!probe||!confirm(`将指纹 ${probe.device_fingerprint} 的设备绑定到此本机？不启动发布、不写入设备。`))return;
    await api('/firmware/bind',{method:'POST',body:JSON.stringify({probe_id:probe.id,confirmation_token:probe.confirmation_token})});
    $('usbIdentity').textContent='设备已绑定。App 可读取标识，不必手工抄写 MAC；尚未联网或确认上屏。';
    probe=null;$('bindPreparationUsb').hidden=true;await loadPreviewDevices();
  }));
})();
