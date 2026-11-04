import { onMounted, ref } from 'vue'
import { api, fmt } from './lib.js'

const states = {
  missing_capsule:'缺冻结决策', no_packet:'未建盲提名任务', missing_input:'输入缺失或不可用',
  pending:'等待领取', claimed:'研究中', expired:'超时未收到结果', multiple_versions:'同批有多个输入版本，暂停比较',
  manifest_mismatch:'冻结数据版本不符', abstained:'已回执，主动放弃提名', late_or_backfilled:'晚到或历史补算，仅展示研究',
  empty_control:'正式 TOP15 与盲选池无交集', awaiting_window:'等待五日窗口或交易日历', awaiting_labels:'已过常规观察期，价格标签待齐',
  different_basis:'双方观察区间或口径不同', paired:'五日价格可比'
}

export default {
  template: `
    <section aria-label="Losna 周度研究对照">
      <h3>Losna 周度研究对照</h3>
      <label>选一周中的任意日期 <input type="date" v-model="chosen" @change="load"></label>
      <button class="ghost" @click="load" :disabled="loading">刷新周度对照</button>
      <p v-if="loading" role="status">正在读取冻结批次…</p>
      <p v-else-if="error" role="alert">{{error}}</p>
      <template v-else-if="report">
        <p>{{report.start}} 至 {{report.end}} · {{report.batches.length}} 个正式批次 · 按 {{report.as_of}} 已保存的证据回看</p>
        <p v-if="report.batches.length">本周盲提名 {{report.summary.nominated}} 项，其中 {{report.summary.outside_formal}} 项在同批正式 TOP15 之外，{{report.summary.with_source}} 项能打开引用事实。五日价格可比 {{report.summary.price_pairs}} 批，首日含费模拟可比 {{report.summary.net_pairs}} 批。</p>
        <p v-if="report.batches.length">{{Object.entries(report.counts).map(([key,n]) => label(key)+' '+n+' 批').join(' · ')}}</p>
        <p v-else>这周没有已发布的正式选股批次。</p>
        <details v-for="b in report.batches" :key="b.run_id">
          <summary>{{b.date}} · {{label(b.state)}}<span v-if="b.difference_pp!=null"> · 盲提名减同池正式组 {{fmt(b.difference_pp)}} 个百分点</span></summary>
          <p>批次 {{b.run_id}} · 盲选任务 {{b.packets}} 个<span v-if="b.pool_count!=null"> · 冻结池 {{b.pool_count}} 只 · 正式 TOP15 中有 {{b.control_symbols.length}} 只进入该池</span></p>
          <p v-if="b.blind">五日价格覆盖：盲提名 {{b.blind.matured}}/{{b.blind.total}} 只，同池正式组 {{b.formal.matured}}/{{b.formal.total}} 只。
            <span v-if="b.difference_pp!=null">从 {{b.blind.basis[0]}} 到 {{b.blind.basis[1]}}，两组均值为 {{fmt(b.blind.mean_pct)}}% 和 {{fmt(b.formal.mean_pct)}}%。</span></p>
          <p v-if="b.control_symbols?.length">同池正式组 {{b.control_symbols.join('、')}}</p>
          <article class="list-item" v-for="r in b.reviews" :key="r.symbol"><div class="list-body">
            <strong>{{r.symbol}} · {{r.in_formal?'与正式 TOP15 重合':'正式 TOP15 之外'}}</strong>
            <p>{{r.reason}}</p><p>研究者提出的待核反证：{{r.counterevidence}}</p>
            <details><summary>查看本次引用的 {{r.evidence.length}} 条冻结事实</summary>
              <p style="overflow-wrap:anywhere" v-for="e in r.evidence" :key="e.id"><code>{{e.id.slice(0,12)}}</code> · {{JSON.stringify(e.fact)}}</p>
            </details>
          </div></article>
          <div v-if="b.paper_pair">
            <p>前向模拟登记于 {{b.paper_pair.recorded_at}}，每组起始 {{b.paper_pair.initial_cash}} 元，同在 {{b.paper_pair.entry_date}} 开盘尝试买入并按当日收盘估值。</p>
            <p v-if="b.paper_pair.status==='marked'">首日含费净值差 {{fmt(b.paper_pair.difference_pp)}} 个百分点。盲提名 {{fmt(b.paper_pair.blind.return_pct)}}%（费用 {{b.paper_pair.blind.fees}} 元），同池正式组 {{fmt(b.paper_pair.formal.return_pct)}}%（费用 {{b.paper_pair.formal.fees}} 元）。</p>
            <p v-else-if="b.paper_pair.status==='missing_facts'">原执行日的行情、费用规则或公司行为核验未齐：{{b.paper_pair.missing_symbols.join('、')}}。</p>
            <p v-else-if="b.paper_pair.status==='pending_close'">等待原执行日收盘。</p>
            <p v-else-if="b.paper_pair.status==='different_cost_rules'">两组归档的成交费用假设与登记时不一致，暂停比较。</p>
            <p v-else>至少一组没有模拟成交，净值差待核。</p>
            <p v-if="b.paper_pair.blind">盲提名实际成交 {{filled(b.paper_pair.blind)}}/{{b.paper_pair.blind.fills.length}} 只；同池正式组 {{filled(b.paper_pair.formal)}}/{{b.paper_pair.formal.fills.length}} 只。</p>
            <details v-if="b.paper_pair.fact_refs"><summary>模型使用的原交易日行情</summary>
              <p v-for="f in b.paper_pair.fact_refs" :key="f.id">{{f.id}} · 归档版本 {{f.revision}} · <code>{{f.hash.slice(0,12)}}</code></p>
            </details>
          </div>
          <p v-else>该批未在下一次可执行开盘前锁定两组模拟目标，含费差留空。</p>
          <p v-if="b.next_check">下次核查：{{b.next_check}}</p>
        </details>
        <p>价格对照使用同批冻结池内的正式 TOP15 成员，按证券等权，仅在双方五日价格标签完整且区间、指标版本一致时展示差值。点名复核单独处理。</p>
        <p v-if="report.formal_model">正式连续模型账户截至 {{report.formal_model.trade_date}}，{{report.formal_model.status==='verified'?'已核净值':'参考净值'}}，自建账以来含费回报 {{report.formal_model.net_return_pct==null?'待核':fmt(report.formal_model.net_return_pct)+'%'}}，累计模拟费用 {{report.formal_model.fees_paid??'待核'}} 元。</p>
        <p v-else>本周暂无正式模型账户估值记录。</p>
        <small>首日模拟反映开盘买入至收盘的持仓净值，净值已扣模拟买入费用，并用含滑点的成交价，未计未来卖出费用。研究者的反证文字仍需独立查证。</small>
      </template>
    </section>`,
  setup() {
    const chosen=ref(''), report=ref(null), loading=ref(false), error=ref('')
    let generation=0
    async function load() {
      const request=++generation
      loading.value=true
      error.value=''
      try {
        const query=chosen.value?'?'+new URLSearchParams({week_of:chosen.value}):''
        const value=await api('/api/research/weekly-contribution'+query)
        if(request===generation) {report.value=value; chosen.value=value.start}
      } catch(e) {
        if(request===generation) error.value='周度对照读取失败：'+e
      } finally {
        if(request===generation) loading.value=false
      }
    }
    onMounted(load)
    return { chosen, report, loading, error, load, fmt, label:key=>states[key]||key,
      filled:arm=>arm.fills.filter(f=>['filled','partially_filled'].includes(f.status)).length }
  }
}
