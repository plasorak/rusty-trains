import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_parquet("analysis.parquet")
print(df)
plt.plot(df.time_s, df.speed_integral_km)
plt.show()