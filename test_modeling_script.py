import polars as pl
from modeling import RegimeAwareEdgeModel

df = pl.DataFrame({
    "spot_vol_annualized": [0.1, 0.5, 0.2, 0.8],
    "spot_trend_lookback": [0.01, -0.05, 0.0, 0.1],
    "future_edge_target": [0.001, 0.002, -0.001, 0.005]
})

model = RegimeAwareEdgeModel()
model.fit(df)
pred = model.predict(df)
print(pred)
