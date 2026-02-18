import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_parquet("simulation.parquet")
plt.plot(df.time_s, df.speed_kmh)
plt.xlabel('time s')
plt.ylabel('speed km/h')
plt.show()