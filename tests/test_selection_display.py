import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

from application.services import SelectionRunService
from jobs.task_control import latest_selection_artifact
from selection.reference_display import wencai_reference


def formal_fixture():
    return {'run_id':'run', 'selection_date':'2026-10-23', 'metadata':{}, 'artifacts':{
        'formal_top15':{'payload':[{'symbol':'600001', 'rank':1}]},
        'formal_top5':{'payload':[{'symbol':'600001', 'rank':1}]},
        'iwencai_openapi_shadow':{'payload':{'groups':[]}},
        'iwencai_openapi_shadow_postclose':{'payload':{
            'expected_market_as_of':'2026-10-22', 'groups':[
                {'name':'低价擒牛', 'status':'semantic_unverified', 'schema_valid':True,
                 'picks':['600001'], 'data_as_of':'20261023',
                 'failure_reasons':['as_of_not_verified']},
                {'name':'主力资金', 'status':'complete', 'semantic_verified':True,
                 'schema_valid':True, 'picks':['600002'], 'data_as_of':'20261022'},
            ]}},
    }}


def test_web_and_agent_share_latest_trial_reference_without_changing_formal_rows(monkeypatch):
    monkeypatch.setenv('WENCAI_REFERENCE_SOURCE', 'openapi_trial')
    formal = formal_fixture()
    with patch('data.research_store.ResearchStore') as store, \
            patch('data.datahub.cached_stock_names', return_value={'600001':'候选甲'}):
        store.return_value.latest_formal_selection.return_value = formal
        web = latest_selection_artifact()['data']
        agent = SelectionRunService(store=store.return_value).latest_formal()['data']
    assert web['wencai_strategy_runs'] == agent['references']['wencai']
    reference = web['wencai_strategy_runs']
    assert reference['ready_groups'] == 1
    assert reference['trial_data_groups'] == 2
    assert reference['strategies']['低价擒牛']['verification_reasons'] == ['as_of_not_verified']
    assert reference['strategies']['低价擒牛']['expected_market_as_of'] == '2026-10-22'
    assert web['final_rows'] == agent['formal_top5'] == [{'symbol':'600001', 'rank':1}]


def test_legacy_reference_and_empty_trial_keep_explicit_state(monkeypatch):
    monkeypatch.setenv('WENCAI_REFERENCE_SOURCE', 'legacy')
    with patch('selection.strategy_cache.artifact_payload', return_value={'version':'legacy'}) as read:
        assert wencai_reference({}) == {'version':'legacy'}
        read.assert_called_once_with({})
    monkeypatch.setenv('WENCAI_REFERENCE_SOURCE', 'openapi_trial')
    result = wencai_reference({})
    assert result['status'] == 'pending'
    assert result['ready_groups'] == result['trial_data_groups'] == 0


def test_browser_reference_projection_distinguishes_data_from_verification():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for browser projection test')
    source = Path(__file__).resolve().parents[1] / 'webui/static/selection-view.js'
    script = '''
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const {referenceGroups} = await import('data:text/javascript;base64,' + readFileSync(process.argv[1]).toString('base64'));
const data = {wencai_strategy_runs:{source_mode:'openapi_trial',strategies:{
  A:{status:'trial_verified',picks:[{symbol:'600001'}]},
  B:{status:'trial_unverified',picks:['600002'],verification_reasons:['as_of_not_verified'],
     result_as_of:'20261023',expected_market_as_of:'2026-10-22'},
  C:{status:'failed',picks:[]},D:{status:'pending',picks:[]}
}}};
const [group] = referenceGroups(data);
assert.equal(group.ready,1);assert.equal(group.dataCount,2);assert.equal(group.trial,true);
assert.equal(group.items[1].verified,false);assert.match(group.items[1].reason,/2026-10-22/);
assert.equal(group.items[2].statusText,'暂不可用');assert.equal(group.items[3].statusText,'等待采样');
assert.deepEqual(referenceGroups({}),[]);
'''
    subprocess.run([node, '--input-type=module', '-e', script, str(source)], check=True, capture_output=True, text=True)
