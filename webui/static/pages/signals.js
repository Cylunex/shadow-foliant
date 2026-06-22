import { reactive, onMounted } from 'vue'
import { api } from '../lib.js'

const ACTION_CN = { buy:'买入', add:'增持', hold:'持有', reduce:'减持', sell:'卖出', watch:'观望', avoid:'回避', alert:'预警' }
const ACTION_CLS = { buy:'red', add:'red', sell:'green', reduce:'green' }   // A股红涨绿跌
const STATUS_CN = { active:'活跃', expired:'过期', invalidated:'被作废', closed:'已关闭', archived:'归档' }
const SOURCE_CN = { analysis:'深度分析', manual:'手动', selection:'选股', monitor:'盯盘' }
const DIMS = [ {k:'action',t:'动作'}, {k:'source_type',t:'来源'}, {k:'horizon',t:'持有周期'} ]

export default {
  template: `
  <div>
    <div class="h1">🎯 决策信号</div>
    <p class="sub">统一信号层:每次分析/选股/盯盘的结构化操作建议(8态动作·进出场计划·生命周期·去重),后验校验真实胜率。</p>
    <div class="tabs" style="margin-bottom:16px">
      <div class="tab" :class="{active:s.tab==='list'}" @click="s.tab='list'">📋 信号列表</div>
      <div class="tab" :class="{active:s.tab==='stats'}" @click="switchStats">📊 胜率统计</div>
    </div>

    <!-- 信号列表 -->
    <div v-if="s.tab==='list'">
      <div class="card">
        <div class="row" style="flex-wrap:wrap;gap:10px;align-items:flex-end">
          <div><label>代码</label><input v-model="s.code" placeholder="可选" style="width:100px"/></div>
          <div><label>状态</label><select v-model="s.status" style="width:100px">
            <option value="active">活跃</option><option value="">全部</option>
            <option value="invalidated">被作废</option><option value="expired">过期</option><option value="closed">已关闭</option>
          </select></div>
          <div><label>动作</label><select v-model="s.action" style="width:90px">
            <option value="">全部</option><option v-for="(v,k) in ACTION_CN" :value="k">{{v}}</option>
          </select></div>
          <div><label>近N天</label><input type="number" v-model.number="s.days" style="width:70px"/></div>
          <button :disabled="s.loading" @click="load">{{s.loading?'加载中…':'查询'}}</button>
          <button class="ghost" :disabled="s.running" @click="runOutcomes" title="对已过持有周期的信号用K线判命中">{{s.running?'校验中…':'跑后验校验'}}</button>
        </div>
        <div v-if="s.ranMsg" class="sub" style="margin-top:6px">{{s.ranMsg}}</div>
      </div>
      <div v-if="s.err" class="err">{{s.err}}</div>
      <div v-if="s.list.length" style="margin-top:12px">
        <table style="width:100%;font-size:13px">
          <thead><tr style="color:var(--muted)">
            <th align=left>代码/名称</th><th align=center>动作</th><th align=center>信心</th>
            <th align=right>参考价</th><th align=right>进场</th><th align=right>止损</th><th align=right>目标</th>
            <th align=center>周期</th><th align=center>来源</th><th align=center>状态</th><th align=left>时间</th>
          </tr></thead>
          <tbody>
            <tr v-for="it in s.list" :key="it.id" style="border-bottom:1px solid var(--bdr)">
              <td><b>{{it.code}}</b> {{it.name||''}}</td>
              <td align=center><b :class="ACTION_CLS[it.action]||''">{{it.action_cn||ACTION_CN[it.action]}}</b></td>
              <td align=center>{{it.confidence||'—'}}<span v-if="it.score" style="color:var(--muted)">/{{it.score}}</span></td>
              <td align=right>{{it.ref_price??'—'}}</td>
              <td align=right>{{it.entry_low?(it.entry_low+(it.entry_high&&it.entry_high!=it.entry_low?'~'+it.entry_high:'')):'—'}}</td>
              <td align=right>{{it.stop_loss??'—'}}</td>
              <td align=right>{{it.target_price??'—'}}</td>
              <td align=center>{{it.horizon}}</td>
              <td align=center>{{SOURCE_CN[it.source_type]||it.source_type}}</td>
              <td align=center><span :style="{color:it.status==='active'?'var(--accent,#4a9)':'var(--muted)'}">{{STATUS_CN[it.status]||it.status}}</span>
                <button v-if="it.status==='active'" class="ghost" style="font-size:11px;margin-left:4px" @click="close(it)">关闭</button>
              </td>
              <td style="color:var(--muted)">{{(it.created_at||'').slice(0,16).replace('T',' ')}}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else-if="!s.loading && s.searched" class="sub" style="margin-top:12px">暂无信号(深度分析一只股后会自动生成一条)</div>
    </div>

    <!-- 胜率统计 -->
    <div v-if="s.tab==='stats'">
      <div class="card">
        <div class="row" style="gap:10px;align-items:flex-end">
          <div><label>分桶维度</label><select v-model="st.dim" @change="loadStats" style="width:120px">
            <option v-for="d in DIMS" :value="d.k">{{d.t}}</option>
          </select></div>
          <div><label>近N天</label><input type="number" v-model.number="st.days" @change="loadStats" style="width:80px"/></div>
        </div>
        <p class="sub" style="margin-top:6px">只统计方向性信号(买/增/卖/减)的命中:hit=方向判对。中性信号(持/观望/回避/预警)不计胜负。</p>
      </div>
      <div v-if="st.err" class="err">{{st.err}}</div>
      <div v-if="st.data" class="card" style="margin-top:12px">
        <table v-if="st.data.buckets&&st.data.buckets.length" style="width:100%">
          <thead><tr style="color:var(--muted)"><th align=left>{{DIMS.find(d=>d.k===st.dim)?.t}}</th><th align=right>样本</th><th align=right>命中</th><th align=right>偏差</th><th align=right>中性</th><th align=right>胜率</th><th align=right>均收益</th></tr></thead>
          <tbody>
            <tr v-for="b in st.data.buckets" :key="b.bucket" style="border-bottom:1px solid var(--bdr)">
              <td><b>{{b.bucket_cn||b.bucket}}</b></td>
              <td align=right>{{b.n}}</td><td align=right>{{b.hit}}</td><td align=right>{{b.miss}}</td><td align=right>{{b.neutral}}</td>
              <td align=right><b>{{b.win_rate_pct!=null?b.win_rate_pct+'%':'—'}}</b></td>
              <td align=right :class="b.avg_ret_pct>0?'red':(b.avg_ret_pct<0?'green':'')">{{b.avg_ret_pct!=null?(b.avg_ret_pct>0?'+':'')+b.avg_ret_pct+'%':'—'}}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="sub">暂无已评信号。先在列表页"跑后验校验"(需信号已过持有周期 + 有足够前向K线)。</div>
      </div>
    </div>
  </div>`,
  setup(){
    const s = reactive({ tab:'list', code:'', status:'active', action:'', days:30,
                         list:[], loading:false, searched:false, running:false, ranMsg:'', err:'' })
    const st = reactive({ dim:'action', days:180, data:null, err:'' })

    async function load(){
      s.loading=true; s.err=''; s.searched=false
      try{
        let qs = `status=${s.status}&limit=200`
        if(s.code) qs+=`&code=${encodeURIComponent(s.code)}`
        if(s.action) qs+=`&action=${s.action}`
        if(s.days) qs+=`&days=${s.days}`
        s.list = await api('/api/signals?'+qs) || []
        s.searched=true
      }catch(e){ s.err=''+e }finally{ s.loading=false }
    }
    async function runOutcomes(){
      s.running=true; s.ranMsg=''
      try{
        const r = await api('/api/signals/outcomes/run?days=90', {method:'POST'})
        s.ranMsg = `后验完成:评估 ${r.evaluated} 条(命中${r.hit}/偏差${r.miss}/中性${r.neutral}),数据不足 ${r.unable},已评跳过 ${r.skipped}`
      }catch(e){ s.ranMsg='后验失败: '+e }finally{ s.running=false }
    }
    async function close(it){
      try{ await api(`/api/signals/${it.id}/status`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({status:'closed'})}); it.status='closed' }
      catch(e){ s.err=''+e }
    }
    async function loadStats(){
      st.err=''
      try{ st.data = await api(`/api/signals/outcomes/stats?dimension=${st.dim}&days=${st.days}`) }
      catch(e){ st.err=''+e }
    }
    function switchStats(){ s.tab='stats'; if(!st.data) loadStats() }

    onMounted(load)
    return { s, st, ACTION_CN, ACTION_CLS, STATUS_CN, SOURCE_CN, DIMS, load, runOutcomes, close, loadStats, switchStats }
  }
}
