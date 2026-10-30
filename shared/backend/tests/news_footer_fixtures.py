from pathlib import Path
from datetime import datetime,timezone,timedelta
import copy
NOW=int(datetime(2026,10,29,12,tzinfo=timezone(timedelta(hours=8))).timestamp())

def announcement(kind='quota_reset',relation='before',typ='exact'):
 target=NOW+14*3600
 e=dict(kind=kind,status='pending',confidence='explicit',target_at=target,expires_at=target,valid_until=target,published_at=NOW-3600,event_id='demo:1234567890000000001',revision='a'*24,canonical_source_url='https://x.com/thsottiaux/status/1234567890000000001',source_author='thsottiaux',post_id='1234567890000000001',time_type=typ,time_relation=relation,plan_scope='unspecified')
 if typ=='unknown':e.update(target_at=None,expires_at=NOW+20*3600,valid_until=NOW+20*3600,expiry_origin_at=NOW-3600)
 if typ in ['date_range','cross_range']:
  lo=NOW+12*3600 if typ=='date_range' else NOW+6*3600;hi=lo+24*3600
  e.update(time_type='date_range',target_at=None,range_start_at=lo,range_end_at=hi,expires_at=hi,valid_until=hi,display_date='2026-10-30' if typ=='date_range' else None)
 return dict(schema_version=2,enabled=True,target_author='thsottiaux',canonical_source_url=e['canonical_source_url'],selected_event=copy.deepcopy(e),**{k:(copy.deepcopy(e) if k==kind else None) for k in ['quota_reset','reset_grant','reset_unspecified']})

def document():
 ai=dict(plan='PLUS',valid_until_date='2026-12-31',codex_7d_used=28,codex_resets_at=NOW+4*86400,reset_credits_available=2,reset_credits_confirmed=True,reset_expiry_list=[NOW+5*86400],deepseek={'CNY':18.80,'USD':0},deepseek_today_tokens=24680,deepseek_today_tokens_complete=True,fresh={k:NOW for k in ['codex','ds_cny','ds_usd','member']},member={'plan':'plus','valid_until_date':'2026-12-31'})
 news=dict(mode='digest',period='morning',schedule_id='morning',date='2026-10-29',issue_title='科技 / AI 早报',text='开源工具更新，关注本地运行与数据管理。\n模型评测重视可复现结果与使用成本。\n墨水屏适合展示低频更新的桌面信息。\n本页为演示内容，预告与数值均非实时。',generated_at=NOW-600,freshness='fresh')
 gold=dict(spot_usd_oz=4000,price_gram_cny=913.08,fx_rate=7.1,price_as_of=NOW,cny_price_as_of=NOW,calculation_schema=2,baseline_schema=2,usd_status='fresh',cny_status='fresh',data_state={'status':'fresh'},baseline_price_as_of=NOW-3600,baseline_reference_kind='first_observation',change_usd_oz_since_reference=12.5,change_cny_g_usd_reference=2.85)
 return dict(schema_ver=1,payload_type='screen',ts=NOW,screen=dict(layout_version=2,font_version='demo',payload_id='demo-stable',display_preferences={},calendar={'days':[{'d':'2026-10-29','workday':True},{'d':'2026-10-30','workday':True}]},device_policy={'news_check':{'schedules':[{'id':'morning','enabled':True,'time':'08:30','day_types':['all'],'display_label':'早报'}]}},pages={'ai':ai,'news_gold':{'news':news,'gold':gold}}))

def cases():
 out=[]
 for name,kind,relation,typ,clock in [('before','quota_reset','before','exact',NOW),('at','quota_reset','at','exact',NOW),('grant-stale','reset_grant','before','exact',NOW+3600),('unknown-reset-offline','quota_reset','before','unknown',NOW+10800),('unknown-grant','reset_grant','before','unknown',NOW),('single-day-range','quota_reset','before','date_range',NOW),('cross-day-range','quota_reset','before','cross_range',NOW),('expired-cache','quota_reset','before','exact',NOW+14*3600),('clock-unknown','quota_reset','before','exact',0)]:
  doc=document();doc['screen']['pages']['ai']['announcements']=announcement(kind,relation,typ)
  if name=='grant-stale':doc['screen']['pages']['ai']['fresh']['ds_cny']=NOW-600
  out.append((name,doc,clock))
 for name in ['cancelled','malformed','no-announcement']:
  doc=document()
  if name!='no-announcement':
   ann=announcement();ann['selected_event']['status']='cancelled' if name=='cancelled' else {'bad':True};doc['screen']['pages']['ai']['announcements']=ann
  out.append((name,doc,NOW))
 return out
