import { createApp, shallowRef, ref, h } from 'vue'
import Stock from './pages/stock.js'
import Fund from './pages/fund.js'
import Portfolio from './pages/portfolio.js'
import Screen from './pages/screen.js'
import Market from './pages/market.js'
import Settings from './pages/settings.js'
import Monitor from './pages/monitor.js'
import Macro from './pages/macro.js'
import History from './pages/history.js'
import Rag from './pages/rag.js'
import Miaoxiang from './pages/miaoxiang.js'
import IndexBar from './indexbar.js'
import Reco from './pages/reco.js'
import Sector from './pages/sector.js'
import Workflow from './pages/workflow.js'
import Briefing from './pages/briefing.js'
import Trade from './pages/trade.js'
import Backtest from './pages/backtest.js'
import Genome from './pages/genome.js'
import Convertible from './pages/convertible.js'

const NAV = [
  { k:'briefing', ic:'☀️', t:'晨报', comp:Briefing },
  // 个人持仓(置顶)
  { k:'port',  ic:'📊', t:'持仓总览', comp:Portfolio },
  { k:'trade', ic:'📋', t:'成交记录', comp:Trade },
  { k:'backtest', ic:'📐', t:'回测', comp:Backtest },
  { k:'genome', ic:'🧬', t:'策略进化', comp:Genome },
  { k:'fund',  ic:'🏦', t:'基金定投', comp:Fund },
  // 分析
  { k:'stock', ic:'🏠', t:'股票分析', comp:Stock },
  { k:'screen', ic:'🎯', t:'选股',     comp:Screen },
  { k:'convertible', ic:'💎', t:'可转债', comp:Convertible },
  { k:'mx',    ic:'🧠', t:'妙想AI',   comp:Miaoxiang },
  { k:'reco',  ic:'⭐', t:'AI推荐',   comp:Reco },
  { k:'workflow', ic:'🧩', t:'AI工作流', comp:Workflow },
  // 行情 / 监测
  { k:'market', ic:'📡', t:'市场',     comp:Market },
  { k:'sector', ic:'📈', t:'板块',     comp:Sector },
  { k:'monitor', ic:'👁️', t:'监测',   comp:Monitor },
  { k:'macro', ic:'🌍', t:'宏观',     comp:Macro },
  // 工具
  { k:'rag', ic:'🔎', t:'语义搜索',   comp:Rag },
  { k:'history', ic:'🕘', t:'历史',   comp:History },
  { k:'settings', ic:'⚙️', t:'设置',  comp:Settings },
]

createApp({
  setup(){
    // 支持 URL hash 路由(可前进后退/刷新保持)
    const initial = (location.hash || '#briefing').slice(1)
    const cur = shallowRef(NAV.find(n=>n.k===initial) || NAV[0])
    const navOpen = ref(false)   // 手机版抽屉开关(MoviePilot 风格:汉堡→左侧滑入)
    const theme = ref(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark')
    function toggleTheme(){
      theme.value = theme.value === 'dark' ? 'light' : 'dark'
      if (theme.value === 'light') document.documentElement.dataset.theme = 'light'
      else document.documentElement.removeAttribute('data-theme')
      try { localStorage.setItem('sf-theme', theme.value) } catch(e) {}
    }
    function go(it){ cur.value = it; location.hash = it.k; navOpen.value = false }
    window.addEventListener('hashchange', ()=>{
      const it = NAV.find(n=>n.k===location.hash.slice(1)); if(it) cur.value = it
    })
    // 注意:返回 fragment(数组),让 .sidebar/.main 直接成为 #app 的子节点,
    // 否则外层 wrapper div 会让 #app 的 display:flex 失效(侧栏/内容垂直堆叠)。
    return () => [
      // 手机版顶部 app-bar(桌面 CSS 隐藏):汉堡 + 当前页标题
      h('div', { class:'appbar' }, [
        h('button', { class:'hamburger', 'aria-label':'菜单', onClick:()=>{ navOpen.value = !navOpen.value } }, '☰'),
        h('div', { class:'appbar-title', translate:'no' }, cur.value.ic + ' ' + cur.value.t),
        h('button', { class:'theme-btn', 'aria-label':'切换主题', onClick:toggleTheme }, theme.value==='dark' ? '🌙' : '☀️'),
      ]),
      // 抽屉遮罩(点击关闭)
      h('div', { class:['nav-backdrop', { show: navOpen.value }], onClick:()=>{ navOpen.value = false } }),
      h('div', { class:['sidebar', { open: navOpen.value }] }, [
        h('div', { class:'brand', translate:'no' }, [ '📈 shadow-foliant', h('small', '智能投研 · FastAPI + Vue') ]),
        ...NAV.map(it => h('div', {
          class:['nav-item', { active: cur.value.k===it.k }], onClick:()=>go(it)
        }, [ h('span',{class:'ic'}, it.ic), h('span', it.t) ])),
        h('button', { class:'theme-btn', onClick:toggleTheme }, theme.value==='dark' ? '🌙 暗色' : '☀️ 亮色'),
        h('div', { class:'tip' }, '新 UI(替代 Streamlit)。功能持续迁移中。'),
      ]),
      h('div', { class:'main' }, [ h(IndexBar), h(cur.value.comp) ]),
    ]
  }
}).mount('#app')
