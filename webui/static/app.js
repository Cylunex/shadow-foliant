import { createApp, shallowRef, ref, h, onMounted, onUnmounted } from 'vue'
import { api } from './lib.js'
import Stock from './pages/stock.js'
import Fund from './pages/fund.js'
import Portfolio from './pages/portfolio.js'
import Screen from './pages/screen.js'
import Market from './pages/market.js'
import Settings from './pages/settings.js'
import Monitor from './pages/monitor.js'
import Macro from './pages/macro.js'
import History from './pages/history.js'
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
import Signals from './pages/signals.js'
import Exit from './pages/exit.js'
import Cockpit from './pages/cockpit.js'

const ICONS = {
  overview:'<path d="M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z"/>',
  sun:'<circle cx="12" cy="12" r="3.5"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/>',
  portfolio:'<path d="M4 19V9m5 10V5m6 14v-7m5 7V3"/>',
  trade:'<path d="M5 7h14M7 3 3 7l4 4m10 2 4 4-4 4M3 17h14"/>',
  broom:'<path d="m15 4 5 5-8 8-5-5 8-8Z"/><path d="m7 12-3 3c-2 2-1 5-1 5s3 1 5-1l3-3"/>',
  fund:'<path d="M3 10h18M5 10v8m4-8v8m6-8v8m4-8v8M3 21h18M12 3l9 5H3l9-5Z"/>',
  stock:'<path d="M4 18 9 12l4 3 7-10"/><path d="M15 5h5v5"/>',
  target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  diamond:'<path d="m12 3 8 6-8 12L4 9l8-6Z"/><path d="M4 9h16M9 3l-2 6 5 12 5-12-2-6"/>',
  brain:'<path d="M9.5 4.5A3.5 3.5 0 0 0 6 8v1a3 3 0 0 0-1 5.8V16a3 3 0 0 0 4.5 2.6M14.5 4.5A3.5 3.5 0 0 1 18 8v1a3 3 0 0 1 1 5.8V16a3 3 0 0 1-4.5 2.6M12 4v16M8 10h4m4 4h-4"/>',
  market:'<path d="M4 18V6m5 12V9m5 9V4m5 14v-6"/>',
  eye:'<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z"/><circle cx="12" cy="12" r="2.5"/>',
  globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  signal:'<path d="M4 17v3m5-8v8m5-13v13m5-17v17"/>',
  test:'<path d="M9 3h6m-5 0v5l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3"/><path d="M7 15h10"/>',
  genome:'<circle cx="8" cy="6" r="2"/><circle cx="16" cy="18" r="2"/><path d="M9.5 7.5 14.5 16.5M16 3c-5 2-8 6-8 11m0 7c5-2 8-6 8-11"/>',
  workflow:'<rect x="3" y="3" width="6" height="6" rx="1"/><rect x="15" y="15" width="6" height="6" rx="1"/><path d="M9 6h4a4 4 0 0 1 4 4v5M15 18h-4a4 4 0 0 1-4-4V9"/>',
  history:'<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5m4-2v6l4 2"/>',
  settings:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H3v-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V3h4v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
}

const NAV_GROUPS = [
  { title:'工作台', items:[
    { k:'cockpit', icon:'overview', t:'项目总览', desc:'Agent 与运行状态', comp:Cockpit },
    { k:'briefing', icon:'sun', t:'晨间策略', desc:'盘前重点与持仓', comp:Briefing },
  ]},
  { title:'资产管理', items:[
    { k:'port', icon:'portfolio', t:'持仓总览', desc:'盈亏、风险与绩效', comp:Portfolio },
    { k:'trade', icon:'trade', t:'成交记录', desc:'交易与归因', comp:Trade },
    { k:'exit', icon:'broom', t:'清仓助手', desc:'退出优先级', comp:Exit },
    { k:'fund', icon:'fund', t:'基金定投', desc:'净值与计划', comp:Fund },
  ]},
  { title:'投研中心', items:[
    { k:'stock', icon:'stock', t:'股票研究', desc:'行情与深度分析', comp:Stock },
    { k:'screen', icon:'target', t:'综合选股', desc:'TOP15 与最终 TOP5', comp:Screen },
    { k:'market', icon:'market', t:'市场全景', desc:'指数、资金与新闻', comp:Market },
    { k:'sector', icon:'stock', t:'板块轮动', desc:'行业与题材', comp:Sector },
    { k:'macro', icon:'globe', t:'宏观周期', desc:'周期与外部变量', comp:Macro },
    { k:'convertible', icon:'diamond', t:'可转债', desc:'双低筛选', comp:Convertible },
    { k:'mx', icon:'brain', t:'妙想 AI', desc:'外部第二意见', comp:Miaoxiang },
  ]},
  { title:'策略与自动化', items:[
    { k:'monitor', icon:'eye', t:'监测盯盘', desc:'条件与提醒', comp:Monitor },
    { k:'reco', icon:'stock', t:'推荐跟踪', desc:'真实盈亏闭环', comp:Reco },
    { k:'signals', icon:'signal', t:'决策信号', desc:'动作与后验', comp:Signals },
    { k:'backtest', icon:'test', t:'策略回测', desc:'组合与归因', comp:Backtest },
    { k:'genome', icon:'genome', t:'策略进化', desc:'上线集与 A/B', comp:Genome },
    { k:'workflow', icon:'workflow', t:'AI 工作流', desc:'多智能体编排', comp:Workflow },
    { k:'history', icon:'history', t:'分析历史', desc:'结果与评估', comp:History },
    { k:'settings', icon:'settings', t:'系统设置', desc:'任务与配置', comp:Settings },
  ]},
]
const NAV = NAV_GROUPS.flatMap(group => group.items)

function iconNode(name){
  return h('span', { class:'nav-icon', innerHTML:`<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]||ICONS.overview}</svg>` })
}

createApp({
  setup(){
    const initial = (location.hash || '#cockpit').slice(1)
    const cur = shallowRef(NAV.find(n=>n.k===initial) || NAV[0])
    const navOpen = ref(false)
    const collapsed = ref(false)
    const theme = ref(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark')
    const health = ref(null)
    let healthTimer = null
    try { collapsed.value = localStorage.getItem('sf-nav-collapsed') === 'true' } catch(e) {}

    function toggleTheme(){
      theme.value = theme.value === 'dark' ? 'light' : 'dark'
      if (theme.value === 'light') document.documentElement.dataset.theme = 'light'
      else document.documentElement.removeAttribute('data-theme')
      try { localStorage.setItem('sf-theme', theme.value) } catch(e) {}
    }
    function toggleCollapsed(){
      collapsed.value = !collapsed.value
      try { localStorage.setItem('sf-nav-collapsed', String(collapsed.value)) } catch(e) {}
    }
    function go(it){ cur.value = it; location.hash = it.k; navOpen.value = false }
    function goHash(hash){ const item=NAV.find(n=>n.k===hash); if(item) go(item) }
    async function loadHealth(){ try { health.value = await api('/api/health') } catch(e) { health.value = null } }
    function onHash(){ const it=NAV.find(n=>n.k===location.hash.slice(1)); if(it) cur.value=it }
    onMounted(()=>{
      window.addEventListener('hashchange', onHash)
      loadHealth(); healthTimer=setInterval(()=>{ if(!document.hidden) loadHealth() }, 60000)
    })
    onUnmounted(()=>{ window.removeEventListener('hashchange', onHash); if(healthTimer) clearInterval(healthTimer) })

    return () => [
      h('div', { class:'appbar' }, [
        h('button', { class:'icon-button hamburger', 'aria-label':'打开菜单', onClick:()=>{ navOpen.value=!navOpen.value } }, '☰'),
        h('div', { class:'mobile-brand' }, [h('span',{class:'brand-mark'},'S'), h('span',cur.value.t)]),
        h('span',{class:['status-dot',health.value?.ready?'ready':'offline']}),
        h('button', { class:'icon-button', 'aria-label':'切换主题', onClick:toggleTheme }, theme.value==='dark'?'☾':'☀'),
      ]),
      h('div', { class:['nav-backdrop',{show:navOpen.value}], onClick:()=>{navOpen.value=false} }),
      h('aside', { class:['sidebar',{open:navOpen.value,collapsed:collapsed.value}] }, [
        h('div',{class:'brand-row'},[
          h('div',{class:'brand',onClick:()=>goHash('cockpit')},[
            h('span',{class:'brand-mark'},'S'),
            h('span',{class:'brand-copy'},[h('b','shadow-foliant'),h('small','A 股智能投研')]),
          ]),
          h('button',{class:'collapse-btn','aria-label':collapsed.value?'展开侧栏':'收起侧栏',onClick:toggleCollapsed},collapsed.value?'›':'‹'),
        ]),
        h('nav',{class:'nav-scroll'},NAV_GROUPS.map(group=>h('section',{class:'nav-section'},[
          h('div',{class:'nav-section-title'},group.title),
          ...group.items.map(it=>h('button',{
            class:['nav-item',{active:cur.value.k===it.k}],title:collapsed.value?it.t:'',onClick:()=>go(it)
          },[iconNode(it.icon),h('span',{class:'nav-copy'},[h('b',it.t),h('small',it.desc)])]))
        ]))),
        h('div',{class:'sidebar-footer'},[
          h('div',{class:'runtime-state'},[
            h('span',{class:['status-dot',health.value?.ready?'ready':'offline']}),
            h('span',{class:'runtime-copy'},[
              h('b',health.value?.ready?'服务正常':'连接不可用'),
              h('small',health.value?.revision?health.value.revision.slice(0,8):'等待健康检查'),
            ])
          ]),
          h('button',{class:'theme-switch',onClick:toggleTheme,title:'切换明暗主题'},theme.value==='dark'?'☾':'☀'),
        ])
      ]),
      h('main',{class:['main',{expanded:collapsed.value}]},[
        h('header',{class:'page-topbar'},[
          h('div',[h('div',{class:'page-kicker'},NAV_GROUPS.find(g=>g.items.includes(cur.value))?.title||'工作台'),h('div',{class:'page-title'},cur.value.t)]),
          h('div',{class:'page-tools'},[
            h('span',{class:'market-legend'},[h('i',{class:'up'}), '红涨', h('i',{class:'down'}), '绿跌']),
            h('button',{class:'icon-button desktop-theme','aria-label':'切换主题',onClick:toggleTheme},theme.value==='dark'?'☾':'☀'),
          ])
        ]),
        h(IndexBar),
        h('div',{class:'page-content'},[h(cur.value.comp)])
      ]),
    ]
  }
}).mount('#app')
