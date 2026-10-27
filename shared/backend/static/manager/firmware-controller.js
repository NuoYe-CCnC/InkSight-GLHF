/* One owner for preparation/build state, including reconnect and other tabs. */
function firmwareStatus(status, kind='构建') {
  return ({queued:`${kind}排队中`,pending:`${kind}待处理`,running:`正在${kind}`,
    completed:`${kind}已完成`,failed:`${kind}失败`,cancelled:`${kind}已取消`,
    interrupted:`${kind}已中断`,stale:'配置或源码已变化，请重新构建',
    waiting_heartbeat:'写入已校验，等待设备联网',network_timeout:'设备联网待确认',
    written_unconfirmed:'写入已校验，待实体屏确认',settings_changed:'设备设置需核对'})[status] || `${kind}状态待检查`;
}
function createFirmwareController({api, schedule=setTimeout, cancel=clearTimeout}) {
  let state={checking:true,requesting:false,data:null,error:null}, sequence=0, timer=null;
  const listeners=new Set(), active=row=>['queued','running'].includes(row?.status);
  function snapshot() {
    const data=state.data, history=data?.history||{}, setup=data?.preparation;
    const build=(history.builds||[]).find(active)||(history.builds||[])[0]||null;
    const preparing=(setup?.jobs||[]).find(active)||null;
    const writing=['flashes','rollbacks'].some(key=>(history[key]||[]).some(row=>active(row)||row.status==='waiting_heartbeat'));
    return {...state,build,preparing,writing,
      canBuild:!!data?.preflight.ready&&!state.checking&&!state.requesting&&!active(build)&&!preparing&&!writing,
      canFlash:build?.status==='completed'&&!build.stale&&!state.checking&&!state.requesting&&!preparing&&!writing};
  }
  function emit(){const value=snapshot();for(const listener of listeners)listener(value)}
  async function refresh({invalidate=false}={}) {
    const current=++sequence;cancel(timer);
    state={...state,checking:true,error:null,...(invalidate?{data:null}:{})};emit();
    try {
      const data=await api('/firmware/status');
      if(current!==sequence)return;
      state={...state,data,checking:false,error:null};emit();
    }catch(error){
      if(current!==sequence)return;
      state={...state,data:null,checking:false,error:error.message};emit();
    }
    if(current===sequence){const value=snapshot();timer=schedule(()=>refresh(),value.error||active(value.build)||value.preparing||value.writing?2500:15000)}
  }
  async function start(confirm) {
    if(!snapshot().canBuild)return;
    const confirmation=state.data.preflight.build_confirmation;
    state={...state,requesting:true};emit();
    try {
      if(!confirm())return;
      await api('/firmware/builds',{method:'POST',body:JSON.stringify({build_confirmation:confirmation})});
    }finally{state={...state,requesting:false};await refresh()}
  }
  async function stopBuild(){const value=snapshot();if(active(value.build)){await api(`/firmware/builds/${encodeURIComponent(value.build.id)}/cancel`,{method:'POST'});await refresh()}}
  function invalidate(){++sequence;cancel(timer);state={...state,data:null,checking:true,error:null};emit()}
  return {refresh,start,stopBuild,invalidate,snapshot,subscribe(listener){listeners.add(listener);listener(snapshot());return()=>listeners.delete(listener)}};
}
if(typeof module!=='undefined')module.exports={createFirmwareController,firmwareStatus};
