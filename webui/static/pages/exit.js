import { reactive, ref, onMounted } from 'vue'
import { api } from '../lib.js'

const TAG = { sell: '🔴清仓', reduce: '🟠减仓', hold: '⚪持有' }
const CATCLS = { '割肉止损': 'green', '止盈锁定': 'red', '破位减仓': 'green', '死钱调出': 'green', '健康保留': '' }

export default {
  template: `
  <div>
    <div class="h1">🧹 清仓决策助手</div>
    <p class="sub">买太多、不知道什么时候清?这里给每只持仓打"清仓紧迫分"排序,持仓过度分散给瘦身目标。
       割肉止损/止盈锁定/破位减仓/死钱调出 综合判断。清仓建议会进决策信号后验(可回看准不准)。</p>
    <div class="card">
      <div class="row" style="gap:12px;align-items:flex-end">
        <div><label>目标持仓数</label><input type="number" v-model.number="s.target" style="width:80px"/></div>
        <button :disabled="s.loading" @click="load">{{s.loading?'分析中…(尾盘数据+AI策略,稍候)':'分析持仓'}}</button>
        <span class="sub" v-if="s.d">持仓 {{s.d.n_holdings}} 只 · 目标 {{s.d.target}} <b v-if="s.d.over_diversified" class="red">· 过度分散</b></span>
      </div>
    </div>
    <div v-if="s.err" class="err">{{s.err}}</div>

    <div v-if="s.d && s.d.text" class="card" style="margin-top:12px">
      <!-- AI 瘦身策略 -->
      <div v-if="aiText" style="background:var(--bg2,#111);border-left:3px solid var(--accent,#4a9);padding:10px 14px;border-radius:6px;margin-bottom:14px">
        <div style="font-weight:600;margin-bottom:6px">🧠 AI 瘦身策略</div>
        <div style="white-space:pre-wrap;color:var(--muted);font:13px/1.7 inherit">{{aiText}}</div>
      </div>
      <!-- 清仓清单 -->
      <table v-if="s.d.items && s.d.items.length" style="width:100%;font-size:13px">
        <thead><tr style="color:var(--muted)">
          <th align=left>动作</th><th align=left>代码/名称</th><th align=center>归类</th>
          <th align=right>紧迫分</th><th align=right>浮盈亏</th><th align=right>持有天</th><th align=left>理由</th>
        </tr></thead>
        <tbody>
          <tr v-for="it in s.d.items" :key="it.code" style="border-bottom:1px solid var(--bdr)"
              :style="{opacity: it.action==='hold'?0.55:1}">
            <td><b>{{TAG[it.action]}}</b></td>
            <td><b>{{it.code}}</b> {{it.name}}</td>
            <td align=center :class="CATCLS[it.category]||''">{{it.category}}</td>
            <td align=right><b :style="{color: it.exit_score>=55?'#e05050':(it.exit_score>=40?'#e0a030':'inherit')}">{{it.exit_score}}</b></td>
            <td align=right :class="it.pnl>0?'red':(it.pnl<0?'green':'')">{{it.pnl!=null?(it.pnl>0?'+':'')+it.pnl.toFixed(1)+'%':'—'}}</td>
            <td align=right>{{it.holding_days??'—'}}</td>
            <td style="max-width:300px;color:var(--muted)">{{it.reason}}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else-if="s.d && !s.d.ok" class="sub" style="margin-top:12px">{{s.d.summary||'无数据'}}</div>
  </div>`,
  setup(){
    const s = reactive({ target: 10, d: null, loading: false, err: '' })
    const aiText = ref('')
    async function load(){
      s.loading = true; s.err = ''
      try{
        s.d = await api('/api/portfolio/exit-advice?target=' + (s.target || 10))
        // 从 text 抽 AI 段(标题之后、"建议处理"之前那段)
        const t = s.d?.text || ''
        aiText.value = t.split('━━')[0].split('\n').slice(1).join('\n').trim()
      }catch(e){ s.err = '' + e }finally{ s.loading = false }
    }
    onMounted(load)
    return { s, aiText, TAG, CATCLS, load }
  }
}
