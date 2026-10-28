"""Same snapshots through Python, real Swift NSMenu actions and firmware C++."""
import copy,json,os,subprocess
from pathlib import Path
import pytest
from core import codex_announcement_view as v
from test_codex_forecast_v2 import snap,row,real_case,NOW,PUB
ROOT=Path(__file__).resolve().parents[3]


@pytest.fixture(scope='module')
def binaries(tmp_path_factory):
    directory=tmp_path_factory.mktemp('forecast-native')
    swift=directory/'menu';cpp=directory/'firmware'
    include=os.environ.get('INKSIGHT_TEST_ARDUINOJSON_INCLUDE')
    if not include:pytest.skip('local ArduinoJson headers required')
    subprocess.run(['swiftc',str(ROOT/'shared/tools/app/macos/MenuQuota.swift'),str(ROOT/'shared/tools/app/macos/ForecastMenu.swift'),str(ROOT/'shared/backend/tests/forecast_menu_test.swift'),'-o',str(swift)],capture_output=True,check=True)
    subprocess.run(['c++','-std=c++17','-I'+include,'-I'+str(ROOT/'shared/firmware/src'),str(ROOT/'shared/backend/tests/codex_announcement_host.cpp'),'-o',str(cpp)],capture_output=True,check=True)
    return swift,cpp


def run(binary,body):
    return json.loads(subprocess.run([str(binary)],input=json.dumps(body),text=True,capture_output=True,check=True).stdout)


@pytest.mark.parametrize('variant',['exact','unknown','date_range','expired','disabled','bad_url','bad_version','bad_expiry','bad_schema','selected_mismatch','bad_target'])
def test_terminal_parity_and_ns_menu_activation(binaries,variant):
    snapshot=snap(real_case());clock=NOW
    if variant in {'unknown','date_range'}:snapshot=snap([row('Codex will reset weekly quota.' if variant=='unknown' else 'Codex will reset weekly quota today 北京时间.')])
    if variant=='expired':clock=snapshot['selected_event']['expires_at']
    if variant=='disabled':snapshot['enabled']=False
    if variant=='bad_url':snapshot['selected_event']['canonical_source_url']='https://x.com/thsottiaux/status/2115273253711409208?evil=1';snapshot['canonical_source_url']=snapshot['selected_event']['canonical_source_url']
    if variant=='bad_version':snapshot['selected_event']['revision']='bad'
    if variant=='bad_expiry':snapshot['selected_event']['valid_until']=NOW
    if variant=='bad_schema':snapshot['schema_version']=True
    if variant=='selected_mismatch':snapshot[snapshot['selected_event']['kind']]=dict(snapshot['selected_event'],revision='f'*24)
    if variant=='bad_target':snapshot['selected_event']['target_at']=True
    selected=v.nearest_event(snapshot,clock)
    menu=run(binaries[0],{'now':clock,'announcements':snapshot})
    firmware=run(binaries[1],{'now':clock,'ai':{'announcements':snapshot},'inspect_selected':True})
    assert menu['enabled']==bool(selected)
    assert menu['label']==('37%*' if selected else '37%')
    assert firmware['selected']==selected
    assert menu['opened']==([selected['canonical_source_url']] if selected else [])
    if selected:assert menu['event_id']==selected['event_id']


@pytest.mark.parametrize('race',['expired','replaced'])
def test_real_menu_click_blocks_removed_or_replaced_event(binaries,race):
    snapshot=snap([row('Codex will reset weekly quota.')])
    result=run(binaries[0],{'now':NOW,'announcements':snapshot,'race':race})
    assert result['enabled'] and result['opened']==[]
