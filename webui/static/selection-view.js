// Presentation only: preserve missing and unverified external reference states.
export function referenceStatus(status) {
  return ({ready:'已核验',trial_verified:'试运行已核验',trial_unverified:'待核验',
    pending:'等待采样',failed:'暂不可用',degraded:'部分可用',empty:'无命中',
    unavailable:'暂不可用'})[status] || '状态待确认'
}

export function referenceGroups(data) {
  return [['问财', data?.wencai_strategy_runs], ['妙想', data?.miaoxiang_strategy_runs]]
    .map(([source, run]) => {
      const items = Object.entries(run?.strategies || {}).map(([name, raw]) => {
        const value = raw || {}
        const picks = (Array.isArray(value.picks) ? value.picks : [])
          .map(p => typeof p === 'string' ? {symbol:p} : p)
          .filter(p => p && /^\d{6}$/.test(p.symbol || p.code || ''))
          .map(p => ({...p, symbol:p.symbol || p.code}))
        const reasons = value.verification_reasons || []
        const reason = ['failed','unavailable'].includes(value.status) ? '本次参考未取得有效结果'
          : reasons.includes('as_of_not_verified')
          ? `日期待核验：返回 ${value.result_as_of || '未知'}，比较日期 ${value.expected_market_as_of || '未知'}`
          : value.status === 'trial_unverified' ? '已返回候选，筛选条件或排名尚未完全核验'
          : value.status === 'pending' ? '等待本次参考结果' : ''
        return {name, picks, status:value.status, statusText:referenceStatus(value.status),
          verified:['ready','trial_verified'].includes(value.status),
          resultAsOf:value.result_as_of, reason}
      })
      return {source, items, trial:run?.source_mode === 'openapi_trial',
        ready:items.filter(x => x.verified).length,
        dataCount:items.filter(x => x.picks.length).length}
    }).filter(group => group.items.length)
}
