"""后缀评级误剔影响面取证(2026-10-10): 修正 rating_order 后缀映射后,
当前快照后缀券有几只进入 TOP25 选入范围(结论: TOP25 内 1 只=127038)。
只读, 不写库。
"""
"""后缀评级误剔影响面: 当前快照 24 只后缀券, 修正映射后有几只进入 TOP25 选入范围。"""
import sys
sys.path.insert(0, '/root/quant')
import pandas as pd
from data.storage import load_meta

snaps = load_meta('cb_snapshots')
snaps['snap_date'] = pd.to_datetime(snaps['snap_date'])
snap = snaps[snaps['snap_date'] == snaps['snap_date'].max()].copy()

base_map = {'AAA': 0, 'AA+': 1, 'AA': 2, 'AA-': 3, 'A+': 4, 'A': 5, 'A-': 6}

def fix_rating(r):
    if isinstance(r, str) and r.endswith('sti'):
        return r[:-3]
    return r

snap['rating_fix'] = snap['rating'].apply(fix_rating)
snap['rn_fix'] = snap['rating_fix'].map(base_map).fillna(99)

df = snap[~snap['code'].astype(str).str.startswith('404')].copy()
df = df[df['rn_fix'] <= 6]
df = df[df['size'] >= 0.5]
df = df[~((df['price'] > 130) & (df['premium'] < 5))]
df['dblow'] = pd.to_numeric(df['dblow'], errors='coerce')
df = df.dropna(subset=['dblow']).sort_values('dblow')
df['rank'] = range(1, len(df) + 1)

suf = snap[snap['rating'].astype(str).str.endswith('sti')]
print('后缀评级券(当前快照):', len(suf), '只')
print(suf[['code', 'name', 'rating', 'dblow', 'size']].to_string(index=False))
print()
for c in suf['code']:
    row = df[df['code'] == c]
    nm = suf.loc[suf['code'] == c, 'name'].iloc[0]
    if not row.empty:
        print(f"{c} {nm}: 修正后入选池, rank={row['rank'].iloc[0]}")
    else:
        print(f"{c} {nm}: 修正后仍被其它过滤剔除(404/规模/强赎/dblow缺失)")
print()
print('TOP25 门槛 dblow =', round(float(df.iloc[24]['dblow']), 2) if len(df) >= 25 else None)
in25 = [c for c in suf['code'] if c in set(df.head(25)['code'])]
in30 = [c for c in suf['code'] if c in set(df.head(30)['code'])]
print('后缀券进入 TOP25 的只数:', len(in25), in25)
print('后缀券进入 TOP30 的只数:', len(in30), in30)
