import { reactive, ref, computed, nextTick, onMounted } from 'vue'
import { api, fmt, fmt4, pct, money, cls, lineChart, useSort } from '../lib.js'

const HCOLS = [
  { k:'code', t:'代码' }, { k:'name', t:'名称' }, { k:'shares', t:'份额' },
  { k:'cost_nav', t:'成本净值' }, { k:'est_nav', t:'最新(估)净值' }, { k:'mv', t:'市值' },
  { k:'pnl', t:'浮盈' }, { k:'pnl_pct', t:'浮盈%' },
  { k:'daily_return', t:'日涨跌' }, { k:'today_pnl', t:'日收益' },
]

export default {
  template: `
  <div>
    <div class="h1">🏦 基金定投</div>
    <p class="sub">我的持有 / 单只查询评分 / 定投回测 / 定投计划。场外开放式基金为主。</p>
    <div class="tabs">
      <div class="tab" :class="{active:tab==='mine'}" @click="tab='mine'">我的基金</div>
      <div class="tab" :class="{active:tab==='query'}" @click="tab='query'">查询 · 回测</div>
      <div class="tab" :class="{active:tab==='screen'}" @click="tab='screen'">基金筛选</div>
      <div class="tab" :class="{active:tab==='val'}" @click="tab='val'">指数估值</div>
      <div class="tab" :class="{active:tab==='plans'}" @click="tab='plans'">定投计划</div>
      <div class="tab" :class="{active:tab==='diag'}" @click="tab='diag'; loadDiag()">组合诊断</div>
      <div class="tab" :class="{active:tab==='cmp'}" @click="tab='cmp'">基金对比</div>
    </div>

    <!-- ============ 我的基金 ============ -->
    <div v-if="tab==='mine'">
      <div v-if="m.err" class="err">{{m.err}}</div>
      <div class="row stretch">
        <div class="card flex1"><div class="k" style="color:var(--muted);font-size:12px">总市值</div>
          <div style="font-size:24px;font-weight:700">{{money(totalMv)}}</div></div>
        <div class="card flex1"><div class="k" style="color:var(--muted);font-size:12px">总成本</div>
          <div style="font-size:24px;font-weight:700">{{money(totalCost)}}</div></div>
        <div class="card flex1"><div class="k" style="color:var(--muted);font-size:12px">总浮盈</div>
          <div style="font-size:24px;font-weight:700" :class="cls(totalPnlPct)">{{money(totalPnl)}} ({{pct(totalPnlPct)}})</div></div>
        <div class="card flex1"><div class="k" style="color:var(--muted);font-size:12px">今日盈亏</div>
          <div style="font-size:24px;font-weight:700" :class="cls(todayPnl)">{{money(todayPnl)}}</div></div>
      </div>
      <div class="card">
        <h3>持有基金 <span style="color:var(--muted);font-weight:400;font-size:12px">(净值读库,秒开;申赎自动维护移动成本)</span>
          <button class="ghost" style="float:right;margin-left:8px" :disabled="m.loading||m.navBusy" @click="loadMine">↻ 刷新</button>
          <button class="ghost" style="float:right" :disabled="m.navBusy" @click="navRefresh" title="抓最新净值入库(每日收盘后跑一次即可)">{{m.navBusy?'更新净值中…':'⟳ 更新净值'}}</button></h3>
        <table v-if="m.holdings&&m.holdings.length">
          <thead><tr><th v-for="c in hcols" :key="c.k" @click="sortBy(c.k)" style="cursor:pointer;user-select:none">{{c.t}}{{arrow(c.k)}}</th><th></th></tr></thead>
          <tbody><tr v-for="h in sortedHoldings" :key="h.code">
            <td>{{h.code}}</td><td>{{h.name||'—'}}</td><td>{{fmt(h.shares)}}</td>
            <td>{{fmt4(h.cost_nav)}}</td><td>{{fmt4(h.est_nav)}}<span v-if="h.nav_date" style="color:var(--muted);font-size:11px"> {{h.nav_date.slice(5)}}</span></td>
            <td>{{money(h.mv)}}</td><td :class="cls(h.pnl_pct)">{{money(h.pnl)}}</td><td :class="cls(h.pnl_pct)">{{pct(h.pnl_pct)}}</td>
            <td :class="cls(h.daily_return)">{{h.daily_return!=null ? pct(h.daily_return/100) : '—'}}</td><td :class="cls(h.today_pnl)">{{h.today_pnl!=null ? money(h.today_pnl) : '—'}}</td>
            <td><span class="link-del" @click="del(h.code)">移除</span></td>
          </tr></tbody>
        </table>
        <div v-else class="loading">暂无持有。用下方"记一笔申赎"录入第一笔申购,持有列表会自动生成。</div>
      </div>

      <div class="card">
        <h3>记一笔申赎 / 定投</h3>
        <div class="row">
          <div><label>基金代码</label><input v-model="t.code" placeholder="如 110011" style="width:120px"/></div>
          <div><label>类型</label><select v-model="t.txn_type"><option>申购</option><option>定投</option><option>赎回</option></select></div>
          <div><label>成交净值</label><input type="number" step="0.0001" v-model.number="t.nav" style="width:110px"/></div>
          <div v-if="t.txn_type!=='赎回'"><label>金额(元)</label><input type="number" v-model.number="t.amount" style="width:120px"/></div>
          <div v-else><label>赎回份额</label><input type="number" v-model.number="t.shares" style="width:120px"/></div>
          <div><label>日期(可选)</label><input v-model="t.trade_date" placeholder="2026-06-06" style="width:120px"/></div>
          <button :disabled="t.busy" @click="addTxn">{{t.busy?'记账中…':'记一笔'}}</button>
        </div>
        <div v-if="t.msg" :class="t.ok?'ok-msg':'err'" style="margin-top:10px">{{t.msg}}</div>
        <p class="sub" style="margin:8px 0 0">申购/定投填金额(自动按净值折份额);赎回填份额。成交净值可填基金查询里的最新净值。</p>
      </div>

      <div class="card" v-if="m.txns&&m.txns.length">
        <h3>申赎流水(最近)</h3>
        <table>
          <thead><tr><th>日期</th><th>代码</th><th>名称</th><th>类型</th><th>净值</th><th>份额</th><th>金额</th><th>持有后份额</th></tr></thead>
          <tbody><tr v-for="(x,i) in m.txns.slice(0,30)" :key="i">
            <td>{{(x.trade_date||x.created_at||'').slice(0,10)}}</td><td>{{x.code}}</td><td>{{x.name||'—'}}</td>
            <td>{{x.txn_type}}</td><td>{{fmt4(x.nav)}}</td><td>{{fmt(x.shares)}}</td><td>{{money(x.amount)}}</td><td>{{fmt(x.pos_shares)}}</td>
          </tr></tbody>
        </table>
      </div>
    </div>

    <!-- ============ 查询 · 回测 ============ -->
    <div v-if="tab==='query'">
      <div class="card"><div class="row">
        <div><label>基金代码</label><input v-model="f.code" @keyup.enter="load" placeholder="如 110011"/></div>
        <button :disabled="f.loading" @click="load">{{f.loading?'加载中…':'查询'}}</button>
      </div></div>
      <div v-if="f.err" class="err">{{f.err}}</div>
      <div v-if="f.info" class="card">
        <h3>{{f.info.name}} <span class="pill">{{f.info.type}}</span>
          <button class="ghost" style="float:right" @click="useForTxn">用此基金记一笔</button></h3>
        <div class="metrics">
          <div class="metric"><div class="k">最新净值</div><div class="v">{{f.info.latest?fmt4(f.info.latest.unit_nav):'—'}}</div></div>
          <div class="metric" v-if="f.info.realtime"><div class="k">盘中估算</div><div class="v" :class="cls(f.info.realtime.gszzl)">{{fmt4(f.info.realtime.gsz)}}</div></div>
          <div class="metric" v-if="f.info.realtime"><div class="k">估算涨跌</div><div class="v" :class="cls(f.info.realtime.gszzl)">{{pct((f.info.realtime.gszzl||0)/100)}}</div></div>
          <div class="metric" v-if="f.score&&f.score.score!=null"><div class="k">综合评分</div><div class="v"><span class="grade">{{f.score.grade}}</span> {{f.score.score}}</div></div>
        </div>
        <div v-if="f.score&&f.score.advice" style="margin-top:12px;color:var(--muted)">{{f.score.advice}}</div>
      </div>
      <div v-if="f.info" class="card">
        <h3>📋 基金档案 <span v-if="ex.loading" class="sub">加载中…</span></h3>
        <div v-if="ex.data">
          <div class="metrics">
            <div class="metric" v-if="ex.data.rating&&ex.data.rating.基金经理"><div class="k">基金经理</div><div class="v">{{ex.data.rating.基金经理}}</div></div>
            <div class="metric" v-if="ex.data.rating&&ex.data.rating.基金公司"><div class="k">基金公司</div><div class="v">{{ex.data.rating.基金公司}}</div></div>
            <div class="metric" v-if="basicOf('最新规模')"><div class="k">规模</div><div class="v">{{basicOf('最新规模')}}</div></div>
            <div class="metric" v-if="basicOf('成立时间')"><div class="k">成立</div><div class="v">{{basicOf('成立时间')}}</div></div>
            <div class="metric" v-if="ex.data.rating&&ex.data.rating.晨星评级"><div class="k">晨星评级</div><div class="v" style="color:#e0a030">{{stars(ex.data.rating.晨星评级)}}</div></div>
            <div class="metric" v-if="ex.data.rating&&ex.data.rating.手续费!=null"><div class="k">手续费</div><div class="v">{{feeFmt(ex.data.rating.手续费)}}</div></div>
          </div>
          <div v-if="thList.length" style="margin-top:12px">
            <b>前十大重仓股</b> <span class="sub">{{(ex.data.top_holdings&&ex.data.top_holdings.quarter)||''}}</span>
            <table style="margin-top:6px"><thead><tr><th>代码</th><th>名称</th><th>占净值</th><th>持仓市值</th></tr></thead>
              <tbody><tr v-for="h in thList" :key="h.code">
                <td>{{h.code}}</td><td>{{h.name}}</td><td>{{h.pct!=null?h.pct+'%':'—'}}</td><td>{{h.mv!=null?money(h.mv):'—'}}</td>
              </tr></tbody></table>
          </div>
          <div v-if="basicOf('投资目标')" class="sub" style="margin-top:10px;line-height:1.6">🎯 {{basicOf('投资目标')}}</div>
        </div>
        <div v-else-if="!ex.loading" class="loading">无档案数据(或数据源暂不可用)。</div>
      </div>
      <div v-if="f.info" class="card">
        <h3>🤖 AI 研判面板 <span style="color:var(--muted);font-weight:400;font-size:12px">业绩/风险/定投适配 多角色</span>
          <button class="ghost" style="float:right" :disabled="ap.loading" @click="loadPanel">{{ap.loading?'研判中…':'生成'}}</button></h3>
        <div v-if="ap.err" class="err">{{ap.err}}</div>
        <div v-if="ap.data">
          <div v-for="(op,role) in ap.data.roles" :key="role" style="padding:9px 0;border-bottom:1px solid var(--line)">
            <b>{{role}}</b><div style="color:var(--muted);margin-top:4px;font-size:13px;white-space:pre-wrap;line-height:1.6">{{op}}</div>
          </div>
          <div v-if="ap.data.synthesis" style="margin-top:10px;padding:11px;background:var(--panel2);border-radius:9px">
            <b style="color:var(--accent)">综合结论</b><div style="margin-top:4px;white-space:pre-wrap;line-height:1.6">{{ap.data.synthesis}}</div>
          </div>
        </div>
      </div>
      <div v-if="f.info" class="card"><h3>单位净值走势</h3><div ref="navc" class="chart"></div></div>
      <div v-if="f.info" class="card">
        <h3>定投回测</h3>
        <div class="row" style="flex-wrap:wrap">
          <div><label>每期金额</label><input type="number" v-model.number="d.amount" style="width:100px"/></div>
          <div><label>周期</label><select v-model="d.period"><option value="monthly">每月</option><option value="biweekly">每两周</option><option value="weekly">每周</option><option value="daily">每日</option></select></div>
          <div v-if="d.period==='monthly'"><label>扣款日</label><input type="number" min="1" max="28" v-model.number="d.day" style="width:70px"/></div>
          <div><label>策略</label><select v-model="d.strategy"><option value="normal">普通定额</option><option value="valuation">估值智能</option><option value="value_avg">价值平均</option></select></div>
          <button :disabled="d.loading" @click="runDca">{{d.loading?'回测中…':'回测'}}</button>
          <button class="ghost" :disabled="d.cmpLoading" @click="runDcaCompare" title="同周期下跑三种策略对比">{{d.cmpLoading?'对比中…':'三策略对比'}}</button>
        </div>
        <div v-if="d.cmp&&d.cmp.length" style="margin-top:12px">
          <b>三策略对比</b> <span class="sub">(每期{{money(d.amount)}}·{{periodCn(d.period)}})</span>
          <table style="margin-top:6px"><thead><tr><th>策略</th><th>累计投入</th><th>期末市值</th><th>收益率</th><th>年化IRR</th><th>最大回撤</th><th>vs一次性</th></tr></thead>
            <tbody><tr v-for="r in d.cmp" :key="r.strategy">
              <td><b>{{stratCn(r.strategy)}}</b></td>
              <td v-if="r.error" colspan="6" class="sub">{{r.error}}</td>
              <template v-else>
                <td>{{money(r.total_invested)}}</td><td :class="cls(r.profit_pct)">{{money(r.final_value)}}</td>
                <td :class="cls(r.profit_pct)">{{pct(r.profit_pct)}}</td><td :class="cls(r.annualized_irr)">{{pct(r.annualized_irr)}}</td>
                <td class="green">{{pct(-(r.max_drawdown||0))}}</td>
                <td><span :class="r.dca_beats_lump?'red':''">{{r.dca_beats_lump?'✅跑赢':'—'}}</span></td>
              </template>
            </tr></tbody></table>
        </div>
        <div v-if="d.res" style="margin-top:16px">
          <div class="metrics">
            <div class="metric"><div class="k">累计投入</div><div class="v">{{money(d.res.total_invested)}}</div></div>
            <div class="metric"><div class="k">期末市值</div><div class="v" :class="cls(d.res.profit_pct)">{{money(d.res.final_value)}}</div></div>
            <div class="metric"><div class="k">收益率</div><div class="v" :class="cls(d.res.profit_pct)">{{pct(d.res.profit_pct)}}</div></div>
            <div class="metric"><div class="k">年化IRR</div><div class="v" :class="cls(d.res.annualized_irr)">{{pct(d.res.annualized_irr)}}</div></div>
            <div class="metric"><div class="k">最大回撤</div><div class="v green">{{pct(-(d.res.max_drawdown||0))}}</div></div>
          </div>
          <div style="margin-top:10px;color:var(--muted)">对比一次性买入收益 {{pct(d.res.lump_sum.profit_pct)}}。
            <b :class="d.res.dca_beats_lump?'red':''">{{d.res.dca_beats_lump?'✅ 定投跑赢':'一次性买入更优'}}</b></div>
          <div ref="dcac" class="chart" style="height:240px;margin-top:8px"></div>
        </div>
      </div>
    </div>

    <!-- ============ 基金筛选 ============ -->
    <div v-if="tab==='screen'">
      <div class="card"><div class="row">
        <div><label>类型</label><select v-model="sc.type"><option v-for="t in fundTypes" :value="t">{{t}}</option></select></div>
        <div><label>排序</label><select v-model="sc.sort"><option v-for="o in sortOpts" :value="o.k">{{o.t}}</option></select></div>
        <div><label>取前 N</label><input type="number" v-model.number="sc.n" style="width:80px"/></div>
        <div><label>近1年≥%</label><input type="number" v-model.number="sc.min1y" placeholder="不限" style="width:80px"/></div>
        <div><label>费率≤%</label><input type="number" step="0.1" v-model.number="sc.maxFee" placeholder="不限" style="width:80px"/></div>
        <button :disabled="sc.loading" @click="runScreen">{{sc.loading?'筛选中…':'筛选'}}</button>
      </div><p class="sub" style="margin:6px 0 0">同类排行(乐咕乐股/东财),收益为百分比。可加近1年收益下限/费率上限。缓存 1h。</p></div>
      <div v-if="sc.err" class="err">{{sc.err}}</div>
      <div v-if="sc.rows" class="card">
        <h3>{{sc.rows.length}} 只</h3>
        <table v-if="sc.rows.length"><thead><tr><th v-for="c in scCols" :key="c" @click="sortSc(c)" style="cursor:pointer;user-select:none">{{scZh(c)}}{{arrowSc(c)}}</th></tr></thead>
          <tbody><tr v-for="(r,i) in sortedSc" :key="i"><td v-for="c in scCols">{{scCell(r,c)}}</td></tr></tbody></table>
        <div v-else class="loading">无结果。</div>
      </div>
    </div>

    <!-- ============ 指数估值 ============ -->
    <div v-if="tab==='val'">
      <div class="card"><div class="row">
        <div><label>宽基指数</label><select v-model="vl.index" @change="runVal"><option v-for="i in valIndexes" :value="i">{{i}}</option></select></div>
        <button :disabled="vl.loading" @click="runVal">{{vl.loading?'加载中…':'查估值'}}</button>
      </div><p class="sub" style="margin:6px 0 0">滚动PE历史分位 → 估值档位 + 定投倍数(低估多投/高估暂停)。驱动估值定投择时。</p></div>
      <div v-if="vl.err" class="err">{{vl.err}}</div>
      <div v-if="vl.data&&vl.data.pe" class="card">
        <h3>{{vl.data.index}} <span class="pill" :class="valCls(vl.data.level)">{{vl.data.level}}</span></h3>
        <div class="metrics">
          <div class="metric"><div class="k">滚动PE</div><div class="v">{{fmt(vl.data.pe)}}</div></div>
          <div class="metric"><div class="k">历史分位</div><div class="v">{{fmt(vl.data.percentile)}}%</div></div>
          <div class="metric"><div class="k">定投倍数</div><div class="v" :class="vl.data.multiplier>1?'red':(vl.data.multiplier<1?'green':'')">{{vl.data.multiplier}}×</div></div>
        </div>
        <p class="sub" style="margin-top:10px">数据 {{vl.data.start}} ~ {{vl.data.end}}（{{vl.data.n}} 个交易日,{{vl.data.source}}）。分位越低越便宜;倍数=建议定投金额相对基准的倍数。</p>
      </div>
    </div>

    <!-- ============ 基金对比 ============ -->
    <div v-if="tab==='cmp'">
      <div class="card"><div class="row" style="flex-wrap:wrap">
        <div style="flex:1;min-width:240px"><label>基金代码(逗号分隔,2-6 只)</label><input v-model="cp.codes" placeholder="如 110011,005827,161725" style="width:100%"/></div>
        <div><label>对比区间</label><select v-model.number="cp.lookback"><option :value="0">最大重叠</option><option :value="365">近1年</option><option :value="1095">近3年</option><option :value="1825">近5年</option></select></div>
        <button :disabled="cp.loading" @click="runCompare">{{cp.loading?'对比中…':'对比'}}</button>
      </div>
      <p class="sub" style="margin:6px 0 0">取各基金净值的**共同时间窗**(成立晚的决定起点),各自归一到 1.0 公平比较。也可从持仓快速选:
        <a v-for="c in mineCodes.slice(0,8)" :key="c" class="link" style="cursor:pointer;margin-right:6px" @click="addCmp(c)">+{{c}}</a></p></div>
      <div v-if="cp.err" class="err">{{cp.err}}</div>
      <div v-if="cp.data&&cp.data.funds&&cp.data.funds.length" class="card">
        <h3>对比结果 <span class="sub">{{cp.data.common_start}} ~ {{cp.data.common_end}} ({{cp.data.n_days}}天)<span v-if="cp.data.skipped&&cp.data.skipped.length">·跳过 {{cp.data.skipped.join(',')}}</span></span></h3>
        <table>
          <thead><tr><th>代码</th><th>名称</th><th>总收益</th><th>年化</th><th>最大回撤</th><th>夏普</th><th>卡玛</th><th>年化波动</th></tr></thead>
          <tbody><tr v-for="(f,i) in cp.data.funds" :key="f.code">
            <td><span :style="{color:cmpColor(i),fontWeight:700}">●</span> {{f.code}}</td>
            <td>{{f.name}}</td>
            <td :class="cls(f.metrics.total_return)">{{pctv(f.metrics.total_return)}}</td>
            <td :class="cls(f.metrics.annualized_return)">{{pctv(f.metrics.annualized_return)}}</td>
            <td class="green">{{pctv(f.metrics.max_drawdown!=null?-f.metrics.max_drawdown:null)}}</td>
            <td>{{f.metrics.sharpe!=null?f.metrics.sharpe.toFixed(2):'—'}}</td>
            <td>{{f.metrics.calmar!=null?f.metrics.calmar.toFixed(2):'—'}}</td>
            <td>{{pctv(f.metrics.annualized_volatility)}}</td>
          </tr></tbody>
        </table>
        <h3 style="margin-top:16px">归一净值叠加</h3>
        <svg v-if="cmpChart.lines" :viewBox="'0 0 '+cmpChart.W+' '+cmpChart.H" style="width:100%;height:240px;background:var(--panel2);border-radius:6px">
          <polyline :points="cmpChart.base" fill="none" stroke="var(--bdr,#555)" stroke-width="1" stroke-dasharray="4 3"/>
          <polyline v-for="ln in cmpChart.lines" :key="ln.code" :points="ln.points" fill="none" :stroke="ln.color" stroke-width="1.6"/>
        </svg>
        <div style="font-size:12px;margin-top:4px">
          <span v-for="(f,i) in cp.data.funds" :key="f.code" :style="{color:cmpColor(i),marginRight:'14px'}">━ {{f.name.slice(0,10)}}</span>
          <span style="color:var(--bdr,#888)">┄ 本金(1.0)</span>
        </div>
      </div>
    </div>

    <!-- ============ 组合诊断 ============ -->
    <div v-if="tab==='diag'">
      <div class="card"><div class="row" style="align-items:center">
        <h3 style="margin:0">🩺 基金组合诊断</h3>
        <button class="ghost" :disabled="dg.loading" @click="loadDiag">{{dg.loading?'诊断中…':'↻ 刷新'}}</button>
        <button class="ghost" :disabled="dg.olLoading" @click="loadOverlap" title="逐只穿透重仓股,较慢">{{dg.olLoading?'穿透中…':'🔍 重仓穿透'}}</button>
        <span class="sub" style="margin-left:auto">市值用库内净值估算·成本口径合并视图</span>
      </div></div>
      <div v-if="dg.err" class="err">{{dg.err}}</div>

      <!-- 股+基 合并资产视图 -->
      <div v-if="cv.data&&cv.data.allocation" class="card">
        <h3>全部资产配置 <span class="sub">(股票+基金,{{cv.data.basis}},合计 {{money(cv.data.total)}})</span></h3>
        <div v-for="(w,k) in cv.data.allocation" :key="k" style="margin:6px 0">
          <div style="display:flex;justify-content:space-between;font-size:13px"><span>{{k}}</span><span>{{pct(w)}} · {{money((cv.data.amounts||{})[k])}}</span></div>
          <div style="height:8px;background:var(--panel2);border-radius:4px;overflow:hidden"><div :style="{width:(w*100)+'%',height:'100%',background:barColor(k)}"></div></div>
        </div>
      </div>

      <div v-if="dg.data&&dg.data.error" class="loading">{{dg.data.error}}</div>
      <template v-if="dg.data&&!dg.data.error">
        <div class="row stretch">
          <div class="card flex1"><div class="k" style="color:var(--muted);font-size:12px">基金数</div><div style="font-size:22px;font-weight:700">{{dg.data.n_funds}}</div></div>
          <div class="card flex1"><div class="k" style="color:var(--muted);font-size:12px">基金总市值</div><div style="font-size:22px;font-weight:700">{{money(dg.data.total_mv)}}</div></div>
          <div class="card flex1"><div class="k" style="color:var(--muted);font-size:12px">集中度 HHI</div><div style="font-size:22px;font-weight:700" :class="dg.data.concentration.hhi>0.25?'red':''">{{fmt(dg.data.concentration.hhi)}}</div></div>
          <div class="card flex1"><div class="k" style="color:var(--muted);font-size:12px">Top1 / Top3</div><div style="font-size:22px;font-weight:700">{{pct(dg.data.concentration.top1)}} / {{pct(dg.data.concentration.top3)}}</div></div>
        </div>

        <div class="row stretch">
          <div class="card flex1">
            <h3>大类配置</h3>
            <div v-for="(w,k) in dg.data.asset_allocation" :key="k" style="margin:6px 0">
              <div style="display:flex;justify-content:space-between;font-size:13px"><span>{{k}}</span><span>{{pct(w)}}</span></div>
              <div style="height:8px;background:var(--panel2);border-radius:4px;overflow:hidden"><div :style="{width:(w*100)+'%',height:'100%',background:barColor(k)}"></div></div>
            </div>
          </div>
          <div class="card flex1">
            <h3>类型配置</h3>
            <div v-for="(w,k) in dg.data.type_allocation" :key="k" style="margin:6px 0">
              <div style="display:flex;justify-content:space-between;font-size:13px"><span>{{k}}</span><span>{{pct(w)}}</span></div>
              <div style="height:8px;background:var(--panel2);border-radius:4px;overflow:hidden"><div :style="{width:(w*100)+'%',height:'100%',background:'#6aa9ff'}"></div></div>
            </div>
          </div>
        </div>

        <div class="card" v-if="dg.data.advice&&dg.data.advice.length">
          <h3>诊断建议</h3>
          <ul style="margin:6px 0;padding-left:20px;line-height:1.9"><li v-for="(a,i) in dg.data.advice" :key="i">{{a}}</li></ul>
        </div>

        <div class="card">
          <h3>持仓权重(前 20)</h3>
          <table v-if="dg.data.holdings&&dg.data.holdings.length">
            <thead><tr><th>代码</th><th>名称</th><th>类型</th><th>大类</th><th>权重</th></tr></thead>
            <tbody><tr v-for="h in dg.data.holdings.slice().sort((a,b)=>b.weight-a.weight).slice(0,20)" :key="h.code">
              <td>{{h.code}}</td><td>{{h.name||'—'}}</td><td>{{h.ftype||'—'}}</td><td>{{h.asset}}</td><td>{{pct(h.weight)}}</td>
            </tr></tbody>
          </table>
        </div>

        <div class="card" v-if="dg.data.stock_overlap">
          <h3>重仓股穿透重叠 <span class="sub">(被≥2只基金共同重仓,按 基金权重×持仓占比 加权)</span></h3>
          <table v-if="dg.data.stock_overlap.length">
            <thead><tr><th>股票</th><th>出现基金数</th><th>合并权重</th></tr></thead>
            <tbody><tr v-for="(o,i) in dg.data.stock_overlap" :key="i"><td>{{o.stock}}</td><td>{{o.in_funds}}</td><td>{{pct(o.combined_weight)}}</td></tr></tbody>
          </table>
          <div v-else class="loading">无显著重叠(或持仓穿透数据不足)。</div>
        </div>
      </template>
    </div>

    <!-- ============ 定投计划 ============ -->
    <div v-if="tab==='plans'">
      <div v-if="pl.err" class="err">{{pl.err}}</div>
      <div class="card">
        <h3>新建定投计划</h3>
        <div class="row">
          <div><label>基金代码</label><input v-model="np.code" placeholder="如 110011" style="width:120px"/></div>
          <div><label>每期金额</label><input type="number" v-model.number="np.amount" style="width:110px"/></div>
          <div><label>周期</label><select v-model="np.period"><option value="monthly">每月</option><option value="weekly">每周</option><option value="daily">每日</option></select></div>
          <div><label>扣款日</label><input type="number" v-model.number="np.day_of" style="width:80px"/></div>
          <div><label>策略</label><select v-model="np.strategy"><option value="normal">普通定额</option><option value="valuation">估值智能</option><option value="value_avg">价值平均</option></select></div>
          <div><label>止盈%</label><input type="number" v-model.number="np.target_profit_pct" placeholder="如 20" style="width:90px"/></div>
          <button :disabled="np.busy" @click="addPlan">{{np.busy?'保存中…':'新建'}}</button>
        </div>
        <p class="sub" style="margin:8px 0 0">⚠️ 计划需后台 jobs_hub 常驻才会到期自动提醒/记账(本机不跑);止盈%留空=不设。</p>
      </div>
      <div class="card">
        <h3>定投计划列表 <button class="ghost" style="float:right" @click="loadPlans">↻ 刷新</button></h3>
        <table v-if="pl.plans&&pl.plans.length">
          <thead><tr><th>代码</th><th>名称</th><th>金额</th><th>周期</th><th>扣款日</th><th>策略</th><th>止盈%</th><th>启用</th><th>操作</th></tr></thead>
          <tbody><tr v-for="p in pl.plans" :key="p.id">
            <td>{{p.code}}</td><td>{{p.name||'—'}}</td><td>{{money(p.amount)}}</td><td>{{periodCn(p.period)}}</td>
            <td>{{p.day_of}}</td><td>{{stratCn(p.strategy)}}</td><td>{{p.target_profit_pct?p.target_profit_pct+'%':'—'}}</td>
            <td>{{p.enabled?'✅':'⏸'}}</td>
            <td><span class="link" style="cursor:pointer" @click="togglePlan(p)">{{p.enabled?'停用':'启用'}}</span>
              &nbsp;<span class="link-del" style="cursor:pointer" @click="delPlan(p)">删除</span></td>
          </tr></tbody>
        </table>
        <div v-else class="loading">暂无定投计划。</div>
      </div>
    </div>
  </div>`,
  setup(){
    const tab = ref('mine')
    // —— 我的基金 ——
    const m = reactive({ holdings:null, txns:null, err:'', loading:false, navBusy:false })
    const { sortBy, arrow, sorted: sortedHoldings } = useSort(()=>m.holdings, 'mv', -1)
    const totalMv = computed(()=> (m.holdings||[]).reduce((s,h)=>s+(h.mv||0),0))
    const totalCost = computed(()=> (m.holdings||[]).reduce((s,h)=>s+(h.cost||0),0))
    const totalPnl = computed(()=> totalMv.value-totalCost.value)
    const totalPnlPct = computed(()=> totalCost.value? totalPnl.value/totalCost.value : null)
    const todayPnl = computed(()=> (m.holdings||[]).reduce((s,h)=>s+(Number(h.today_pnl)||0),0))
    async function loadMine(){
      m.loading=true; m.err=''
      try{ m.holdings = await api('/api/fund/holdings') }catch(e){ m.err=''+e }
      try{ m.txns = await api('/api/fund/transactions') }catch(e){}
      m.loading=false
    }
    const t = reactive({ code:'', txn_type:'申购', nav:null, amount:null, shares:null, trade_date:'', busy:false, ok:false, msg:'' })
    async function addTxn(){
      if(!t.code || !t.nav){ t.ok=false; t.msg='请填代码和成交净值'; return }
      t.busy=true; t.msg=''
      try{
        const body={ code:t.code, txn_type:t.txn_type, nav:t.nav, fee:0, trade_date:t.trade_date||null }
        if(t.txn_type==='赎回') body.shares=t.shares; else body.amount=t.amount
        const r = await api('/api/fund/transaction',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
        t.ok=true; t.msg='已记账:'+t.txn_type+' '+t.code+(r&&r.pos_shares!=null?(' · 持有份额 '+fmt(r.pos_shares)):'')
        t.amount=null; t.shares=null
        await loadMine()
      }catch(e){ t.ok=false; t.msg=''+e }finally{ t.busy=false }
    }
    async function del(code){
      try{ await api('/api/fund/holdings/'+code,{method:'DELETE'}); await loadMine() }catch(e){ m.err=''+e }
    }
    async function navRefresh(){
      m.navBusy=true; m.err=''
      try{ const r = await api('/api/fund/nav-refresh',{method:'POST'}); await loadMine() }
      catch(e){ m.err=''+e }finally{ m.navBusy=false }
    }
    function useForTxn(){ t.code=f.code; if(f.info&&f.info.latest) t.nav=f.info.latest.unit_nav; tab.value='mine' }

    // —— 查询 · 回测 ——
    const f = reactive({ code:'110011', info:null, score:null, err:'', loading:false })
    const d = reactive({ amount:1000, period:'monthly', day:5, strategy:'normal', res:null, loading:false, cmp:null, cmpLoading:false })
    const navc = ref(), dcac = ref()
    // —— 基金档案(重仓股/评级/基本信息,异步懒加载,不阻塞主查询)——
    const ex = reactive({ data:null, loading:false })
    const thList = computed(()=> (ex.data&&ex.data.top_holdings&&ex.data.top_holdings.holdings)||[])
    const basicOf = k => (ex.data&&ex.data.basic&&ex.data.basic[k])||null
    const stars = v => { const n=Math.round(Number(v)||0); return n>0?'★'.repeat(n):'—' }
    const feeFmt = v => v==null?'—':((Number(v)<1?Number(v)*100:Number(v)).toFixed(2)+'%')  // 评级表费率为小数(0.0015=0.15%)
    async function loadExtras(code){
      ex.loading=true; ex.data=null
      try{ ex.data = await api('/api/fund/'+code+'/extras') }catch(e){}
      finally{ ex.loading=false }
    }
    async function load(){
      f.loading=true; f.err=''; f.info=null; f.score=null; d.res=null; ex.data=null
      try{
        f.info = await api('/api/fund/'+f.code)
        const nv = await api('/api/fund/'+f.code+'/nav')
        await nextTick(); lineChart(navc.value, nv, 'date','nav','#26c281')
        f.score = await api('/api/fund/'+f.code+'/score?extras=false')
        loadExtras(f.code)   // 不 await:档案数据慢,后到后填
      }catch(e){ f.err=''+e }finally{ f.loading=false }
    }
    async function runDca(){
      d.loading=true; d.res=null
      try{
        d.res = await api('/api/fund/dca-backtest',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({code:f.code, amount:d.amount, period:d.period, strategy:d.strategy, day:d.day})})
        if(d.res.equity_curve){ await nextTick(); lineChart(dcac.value, d.res.equity_curve,'date','value','#f5a623') }
      }catch(e){ f.err=''+e }finally{ d.loading=false }
    }
    async function runDcaCompare(){
      d.cmpLoading=true; d.cmp=null
      try{
        const r = await api('/api/fund/dca-compare',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({code:f.code, amount:d.amount, period:d.period, day:d.day})})
        d.cmp = r.results
      }catch(e){ f.err=''+e }finally{ d.cmpLoading=false }
    }

    // —— 定投计划 ——
    const pl = reactive({ plans:null, err:'' })
    const np = reactive({ code:'', amount:1000, period:'monthly', day_of:1, strategy:'normal', target_profit_pct:null, busy:false })
    const periodCn = p => ({monthly:'每月',biweekly:'每两周',weekly:'每周',daily:'每日'}[p]||p)
    const stratCn = s => ({normal:'普通定额',valuation:'估值智能',value_avg:'价值平均'}[s]||s)
    async function loadPlans(){
      pl.err=''
      try{ pl.plans = await api('/api/fund/plans') }catch(e){ pl.err=''+e }
    }
    async function addPlan(){
      if(!np.code){ pl.err='请填基金代码'; return }
      np.busy=true; pl.err=''
      try{
        await api('/api/fund/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
          code:np.code, amount:np.amount, period:np.period, day_of:np.day_of, strategy:np.strategy,
          target_profit_pct:np.target_profit_pct||null })})
        await loadPlans()
      }catch(e){ pl.err=''+e }finally{ np.busy=false }
    }
    async function togglePlan(p){
      pl.err=''
      try{ await api('/api/fund/plan/'+p.id+'/toggle',{method:'POST'}); await loadPlans() }
      catch(e){ pl.err=''+e }
    }
    async function delPlan(p){
      if(!confirm('删除定投计划 '+(p.name||p.code)+'?')) return
      pl.err=''
      try{ await api('/api/fund/plan/'+p.id,{method:'DELETE'}); await loadPlans() }
      catch(e){ pl.err=''+e }
    }

    // —— 组合诊断 ——
    const dg = reactive({ data:null, err:'', loading:false, olLoading:false })
    const cv = reactive({ data:null })
    const BAR_COLORS = { '权益':'#e0533d', '固收':'#3da35d', '现金':'#888', '配置':'#b07ad0', '其他':'#c0a020', '未知':'#666', '股票':'#e0533d' }
    const barColor = k => { for(const key in BAR_COLORS){ if((k||'').includes(key)) return BAR_COLORS[key] } return '#6aa9ff' }
    async function loadDiag(){
      dg.loading=true; dg.err=''
      try{
        const [d, c] = await Promise.all([
          api('/api/fund/diagnose'),
          api('/api/fund/combined-view').catch(()=>null)
        ])
        dg.data = d; cv.data = c
      }catch(e){ dg.err=''+e }finally{ dg.loading=false }
    }
    async function loadOverlap(){
      dg.olLoading=true; dg.err=''
      try{ dg.data = await api('/api/fund/diagnose?overlap=true') }
      catch(e){ dg.err=''+e }finally{ dg.olLoading=false }
    }

    // —— 基金对比 ——
    const CMP_COLORS = ['#e0533d','#3da35d','#e0a030','#6aa9ff','#b07ad0','#40c0c0']
    const cmpColor = i => CMP_COLORS[i % CMP_COLORS.length]
    const cp = reactive({ codes:'', lookback:0, data:null, err:'', loading:false })
    const mineCodes = computed(()=> (m.holdings||[]).map(h=>h.code))
    const pctv = pct
    function addCmp(c){ const set=cp.codes.split(/[,，\s]+/).map(x=>x.trim()).filter(Boolean); if(!set.includes(c)) set.push(c); cp.codes=set.join(',') }
    async function runCompare(){
      const cs=cp.codes.split(/[,，\s]+/).map(x=>x.trim()).filter(Boolean)
      if(cs.length<2){ cp.err='请至少填 2 只基金代码'; return }
      cp.loading=true; cp.err=''; cp.data=null
      try{ cp.data = await api('/api/fund/compare?codes='+encodeURIComponent(cs.join(','))+'&lookback_days='+cp.lookback) }
      catch(e){ cp.err=''+e }finally{ cp.loading=false }
    }
    const cmpChart = computed(()=>{
      const fs = cp.data&&cp.data.funds; if(!fs||!fs.length) return {lines:null}
      const W=640,H=220,pad=8
      let tmin=Infinity,tmax=-Infinity,lo=Infinity,hi=-Infinity
      fs.forEach(f=>(f.curve||[]).forEach(p=>{const t=Date.parse(p.date);if(t<tmin)tmin=t;if(t>tmax)tmax=t;if(p.nav<lo)lo=p.nav;if(p.nav>hi)hi=p.nav}))
      if(!isFinite(tmin)) return {lines:null}
      if(lo>1)lo=1; if(hi<1)hi=1; if(hi===lo)hi=lo+1; if(tmax===tmin)tmax=tmin+1
      const X=t=>pad+(t-tmin)/(tmax-tmin)*(W-2*pad)
      const Y=v=>H-pad-(v-lo)/(hi-lo)*(H-2*pad)
      const lines=fs.map((f,i)=>({code:f.code,color:cmpColor(i),points:(f.curve||[]).map(p=>X(Date.parse(p.date))+','+Y(p.nav)).join(' ')}))
      const baseY=Y(1.0)
      return {W,H,lines,base:pad+','+baseY+' '+(W-pad)+','+baseY}
    })

    // —— AI 研判面板 ——
    const ap = reactive({ data:null, err:'', loading:false })
    async function loadPanel(){
      ap.loading=true; ap.err=''; ap.data=null
      try{ ap.data = await api('/api/fund/'+f.code+'/ai-panel',{method:'POST'}) }
      catch(e){ ap.err=''+e }finally{ ap.loading=false }
    }
    // —— 基金筛选 ——
    const FUND_TYPES = ['股票型','混合型','债券型','指数型','QDII','LOF','FOF','全部']
    const SORT_OPTS = [{k:'r_1y',t:'近1年'},{k:'r_3m',t:'近3月'},{k:'r_6m',t:'近6月'},{k:'r_3y',t:'近3年'},{k:'r_ytd',t:'今年'},{k:'r_1m',t:'近1月'}]
    const SC_COLS = ['code','name','r_1m','r_3m','r_6m','r_1y','r_3y','fee']
    const SC_ZH = {code:'代码',name:'名称',r_1m:'近1月',r_3m:'近3月',r_6m:'近6月',r_1y:'近1年',r_3y:'近3年',fee:'费率'}
    const sc = reactive({ type:'股票型', sort:'r_1y', n:20, min1y:null, maxFee:null, rows:null, err:'', loading:false })
    const { sortBy:sortSc, arrow:arrowSc, sorted:sortedSc } = useSort(()=> sc.rows, '', 1)
    async function runScreen(){
      sc.loading=true; sc.err=''; sc.rows=null
      try{
        let u = '/api/fund/screen?type='+encodeURIComponent(sc.type)+'&sort_by='+sc.sort+'&top_n='+sc.n
        if(sc.min1y!=null && sc.min1y!=='') u += '&min_1y='+sc.min1y
        if(sc.maxFee!=null && sc.maxFee!=='') u += '&max_fee='+sc.maxFee
        sc.rows = await api(u)
      }catch(e){ sc.err=''+e }finally{ sc.loading=false }
    }
    const scZh = c => SC_ZH[c]||c
    const scCell = (r,c) => { const v=r[c]; if(v==null) return '—'; if(c.startsWith('r_')||c==='fee') return (+v).toFixed(2)+'%'; return v }
    // —— 指数估值 ——
    const VAL_INDEXES = ['上证50','沪深300','中证500','中证1000','创业板指','科创50']
    const vl = reactive({ index:'沪深300', data:null, err:'', loading:false })
    async function runVal(){
      vl.loading=true; vl.err=''
      try{ vl.data = await api('/api/fund/valuation?index='+encodeURIComponent(vl.index)) }
      catch(e){ vl.err=''+e }finally{ vl.loading=false }
    }
    const valCls = lv => /低估/.test(lv||'')?'red':(/高估/.test(lv||'')?'green':'')

    onMounted(()=>{ loadMine(); loadPlans() })
    return { tab, m, hcols:HCOLS, sortedHoldings, sortBy, arrow, totalMv, totalCost, totalPnl, totalPnlPct, todayPnl,
             loadMine, navRefresh, t, addTxn, del, useForTxn,
             f, d, navc, dcac, load, runDca, runDcaCompare, pl, np, periodCn, stratCn, loadPlans, addPlan, togglePlan, delPlan,
             ap, loadPanel,
             ex, thList, basicOf, stars, feeFmt,
             dg, cv, loadDiag, loadOverlap, barColor,
             cp, runCompare, cmpColor, cmpChart, pctv, mineCodes, addCmp,
             sc, fundTypes:FUND_TYPES, sortOpts:SORT_OPTS, scCols:SC_COLS, scZh, scCell, sortSc, arrowSc, sortedSc, runScreen,
             vl, valIndexes:VAL_INDEXES, runVal, valCls,
             fmt, fmt4, pct, money, cls }
  }
}
