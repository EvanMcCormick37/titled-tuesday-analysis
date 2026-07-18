from datetime import date
import warnings
import numpy as np
import pandas as pd
import sys
from pathlib import Path

# Add the parent directory to the search path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))
from bootstrap_mc_kalshi import load_and_prepare, build_player_pool, build_player_pool_score, run_simulation, run_simulation_score, build_results, USERNAME_TO_PLAYER, PLAYER_TO_USERNAME

warnings.filterwarnings('ignore')
pd.set_option({
    'display.max_rows':300,
    'display.max_columns':300,
    'display.max_colwidth':300
})

CUT_PLAYERS = [
'Matthias Bluebaum',
'Levon Aronian',
'Yagiz Kaan Erdogmus',
'Jose Martinez',
'Le Quang Liem',
'Daniel Naroditsky',
'Hans Niemann',
'Arjun Erigaisi',
'Pranesh Munirethinam',
'Dmitry Andreikin',
'Nihal Sarin',
'Alireza Firouzja',
'Nodirbek Abdusattorov'
]
KEEP_PLAYERS = []
N_SIMS = 100_000


def main():
    df_tourneys = pd.read_csv(BASE_DIR / "data/titled_tuesday_tournaments.csv")
    df_standings = pd.read_csv(BASE_DIR / "data/titled_tuesday_standings.csv")
    df_tourneys = df_tourneys[df_tourneys["date"] > "2022-02-01"]
    df_standings = df_standings[df_standings["date"] > "2022-02-01"]

    df, p_participate, app_counts = load_and_prepare(CUT_PLAYERS, KEEP_PLAYERS, attendance_model='decay')
    players, p_play, hist_pcts, hist_wts = build_player_pool_score(
        df, p_participate, app_counts, min_appearances=5
    )

    print(f'Pool: {len(players):,} players | simulating {N_SIMS:,} tournaments...')
    plays_ct, topn_ct = run_simulation_score(players, p_play, hist_pcts, hist_wts, n_sims=N_SIMS)
    results = build_results(players, p_play, plays_ct, topn_ct, n_sims=N_SIMS)
    results.to_csv(BASE_DIR / "data/model_predictions/latest.csv")


if __name__ == "__main__":
    main()