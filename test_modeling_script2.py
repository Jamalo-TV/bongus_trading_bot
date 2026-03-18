import polars as pl
from modeling import RegimeAwareEdgeModel

df_train = pl.DataFrame({
    "spot_vol_annualized": [0.1],
    "spot_trend_lookback": [0.01],
    "future_edge_target": [0.001]
})

df_test = pl.DataFrame({
    "spot_vol_annualized": [0.8],
    "spot_trend_lookback": [0.1]
})

model = RegimeAwareEdgeModel()
model.fit(df_train)
pred = model.predict(df_test)
print(pred)
