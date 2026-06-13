import { ref, onMounted, onUnmounted } from 'vue'
import { api, cls } from './lib.js'

// 顶部大盘指数条(常驻每页)。新浪实时,前端 30s 轮询。A股红涨绿跌。
export default {
  template: `
  <div v-if="items.length" class="idxbar">
    <div v-for="x in items" :key="x.name" class="idx" :title="(x.change_amt>=0?'+':'')+x.change_amt.toFixed(2)">
      <span class="idx-n">{{x.name}}</span>
      <span class="idx-v" :class="cls(x.change_pct)">{{x.value.toFixed(2)}}</span>
      <span class="idx-c" :class="cls(x.change_pct)">{{(x.change_pct>=0?'+':'')+x.change_pct.toFixed(2)}}%</span>
    </div>
  </div>`,
  setup(){
    const items = ref([])
    let timer = null
    async function load(){ try{ items.value = await api('/api/market/indices') || [] }catch(e){} }
    // 标签页隐藏时不轮询(省请求);切回前台立即刷新
    onMounted(()=>{ load(); timer = setInterval(()=>{ if(!document.hidden) load() }, 30000)
      document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) load() }) })
    onUnmounted(()=>{ if(timer) clearInterval(timer) })
    return { items, cls }
  }
}
