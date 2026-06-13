"""基金筛选器(阶段二)—— 从同类排行榜按业绩/规模/费率等条件过滤排序。

数据源:akshare `fund_open_fund_rank_em`(东财同类排行,含近1周/1月/3月/6月/1年/3年/今年来收益)。
设计:列名按关键词匹配(东财列名偶有变动),阈值过滤 + 排序 + TopN。失败返回空。
"""

from __future__ import annotations

from typing import List, Dict, Optional

import pandas as pd

import fund_data

# 排行表里"近X"收益列的关键词 → 统一键
_RETURN_COLS = {
    '近1周': 'r_1w', '近1月': 'r_1m', '近3月': 'r_3m', '近6月': 'r_6m',
    '近1年': 'r_1y', '近2年': 'r_2y', '近3年': 'r_3y', '今年来': 'r_ytd',
    '成立来': 'r_since',
}


def _normalize_rank(df: pd.DataFrame) -> pd.DataFrame:
    """把东财排行表规整:代码/简称 + 标准化的收益列(float)。"""
    out = pd.DataFrame()
    code_col = next((c for c in df.columns if c == '基金代码' or '代码' in c), None)
    name_col = next((c for c in df.columns if c == '基金简称' or '简称' in c), None)
    out['code'] = df[code_col].astype(str).str.zfill(6) if code_col else None
    out['name'] = df[name_col] if name_col else None
    for cn, key in _RETURN_COLS.items():
        col = next((c for c in df.columns if cn in c), None)
        if col is not None:
            out[key] = pd.to_numeric(df[col], errors='coerce')  # 东财已是百分数数值
    fee_col = next((c for c in df.columns if '手续费' in c or '费率' in c), None)
    if fee_col is not None:
        out['fee'] = pd.to_numeric(df[fee_col].astype(str).str.replace('%', ''), errors='coerce')
    return out


def screen_funds(fund_type: str = '股票型', sort_by: str = 'r_1y', top_n: int = 20,
                 min_1y: Optional[float] = None, min_3y: Optional[float] = None,
                 max_fee: Optional[float] = None) -> List[Dict]:
    """同类排行筛选。
    Args:
        fund_type: 全部/股票型/混合型/债券型/指数型/QDII/LOF/FOF
        sort_by:   排序键(r_1w/r_1m/r_3m/r_6m/r_1y/r_2y/r_3y/r_ytd/r_since),降序
        top_n:     取前 N
        min_1y/min_3y: 近1年/近3年收益(%)下限;max_fee: 手续费(%)上限
    Returns: 记录列表(code/name + 各期收益)。失败返回 []。
    """
    df = fund_data.get_rank(fund_type)
    if df is None or df.empty:
        return []
    n = _normalize_rank(df)
    if n.empty or 'code' not in n.columns:
        return []
    if min_1y is not None and 'r_1y' in n.columns:
        n = n[n['r_1y'] >= min_1y]
    if min_3y is not None and 'r_3y' in n.columns:
        n = n[n['r_3y'] >= min_3y]
    if max_fee is not None and 'fee' in n.columns:
        n = n[(n['fee'].isna()) | (n['fee'] <= max_fee)]
    if sort_by in n.columns:
        n = n.sort_values(sort_by, ascending=False, na_position='last')
    return n.head(top_n).where(pd.notna(n), None).to_dict('records')


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    res = screen_funds('股票型', sort_by='r_1y', top_n=5, min_1y=0)
    print(f'命中 {len(res)} 只:')
    for r in res[:5]:
        print(' ', r.get('code'), r.get('name'), '近1年', r.get('r_1y'))
