"""Visualization helpers for Titled Tuesday model predictions and P&L."""

import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import numpy as np
import seaborn as sns


def plot_top_players_by_chance(results, username_to_player, ns=(1, 3, 5, 8)):
    """Horizontal bar charts of P(top-N) for the top 30 players per N.

    Bars are colored by conditional probability (P_top{N}_given_play).
    Inline labels show overall probability and conditional probability.
    """
    cmap = plt.get_cmap('plasma')
    fig, axes = plt.subplots(1, len(ns), figsize=(6 * len(ns), 8))
    if len(ns) == 1:
        axes = [axes]

    _label = {1: 'Win', 3: 'Top 3', 5: 'Top 5', 8: 'Top 8', 10: 'Top 10'}

    for ax, n in zip(axes, ns):
        col_chance = f'P_top{n}'
        col_ip     = f'P_top{n}_given_play'

        df_plot = results.nlargest(30, col_chance).reset_index()
        norm    = mcolors.Normalize(vmin=df_plot[col_ip].min(), vmax=df_plot[col_ip].max())
        colors  = cmap(norm(df_plot[col_ip]))
        names   = [username_to_player.get(u, u) for u in df_plot['username']]

        sns.barplot(data=df_plot, x=col_chance, y=names, ax=ax, palette=colors)

        ax.set_title(f'Top Players by Chance to {_label.get(n, f"Make Top {n}")}', fontweight='bold')
        max_w = df_plot[col_chance].max() * 1.25
        ax.set_xlim(0, max_w)
        ax.set_xlabel('Result Probability')
        ax.set_ylabel('')
        ax.xaxis.set_major_formatter(mtick.PercentFormatter(1.0, decimals=1))

        for i, patch in enumerate(ax.patches):
            y = patch.get_y() + patch.get_height() / 2
            chance_val = df_plot.iloc[i][col_chance]
            ip_val     = df_plot.iloc[i][col_ip]
            ax.text(patch.get_width() + max_w * 0.02, y, f'{chance_val*100:.1f}%',
                    va='center', ha='left', fontsize=10)
            ax.text(max_w * 0.02, y, f'{ip_val*100:.1f}ip%',
                    va='center', ha='left', fontsize=10, color='white')

    plt.tight_layout()
    plt.show()


def plot_yes_no_prices(results, username_to_player, ns=(1, 3, 8)):
    """Diverging bar chart of estimated YES / NO fair prices for the top 30 players.

    Prices assume a 33% house margin: YES = P × 2/3, NO = (1-P) × 2/3.
    """
    fig, axes = plt.subplots(1, len(ns), figsize=(6 * len(ns), 8))
    if len(ns) == 1:
        axes = [axes]

    _label = {1: 'Win', 3: 'Top 3', 5: 'Top 5', 8: 'Top 8', 10: 'Top 10'}

    for ax, n in zip(axes, ns):
        col_chance = f'P_top{n}'
        df_plot = results.nlargest(30, col_chance).reset_index()

        df_plot['Yes_Price'] = np.round(df_plot[col_chance] * (2 / 3) * 100, 1)
        df_plot['No_Price']  = np.round((1 - df_plot[col_chance] * (3 / 2)) * 100, 1).clip(lower=0)

        ax.barh(df_plot.index, df_plot['Yes_Price'], height=0.8, color='#2ca02c', label='Yes')
        ax.barh(df_plot.index, df_plot['No_Price'], left=-df_plot['No_Price'], height=0.8,
                color='#d62728', label='No')
        ax.set_yticks([])
        ax.invert_yaxis()
        ax.set_title(f'Fair Prices: {_label.get(n, f"Top {n}")} (33% margin)', fontweight='bold')

        for i, row in df_plot.iterrows():
            name = username_to_player.get(row['username'], row['username'])
            ax.text(row['Yes_Price'], i, row['Yes_Price'], va='center', ha='left', fontsize=8)
            ax.text(-row['No_Price'], i, row['No_Price'], va='center', ha='right', fontsize=8)
            ax.text(0, i, name, va='center', ha='right', fontsize=8, color='white')

    plt.tight_layout()
    plt.show()


def plot_ip_advantage(results, username_to_player, ns=(1, 3, 8)):
    """Bar charts of IP-advantage (P_top{N}_given_play / P_top{N}) for qualified players.

    Only players with P_top{N} > 1% are included. Bars colored by overall probability.
    """
    cmap = plt.get_cmap('plasma')
    fig, axes = plt.subplots(1, len(ns), figsize=(8 * len(ns), 8))
    if len(ns) == 1:
        axes = [axes]

    _label = {1: 'Win', 3: 'Top 3', 5: 'Top 5', 8: 'Top 8', 10: 'Top 10'}

    for ax, n in zip(axes, ns):
        col_p   = f'P_top{n}'
        col_ip  = f'P_top{n}_given_play'
        col_adv = f'ip_adv_top{n}'

        df_plot = results[results[col_p] > 0.01].nlargest(25, col_adv).reset_index()
        df_plot['name'] = df_plot['username'].map(lambda u: username_to_player.get(u, u))

        norm   = mcolors.Normalize(vmin=df_plot[col_p].min(), vmax=df_plot[col_p].max())
        colors = cmap(norm(df_plot[col_p]))

        sns.barplot(data=df_plot, x=col_adv, y='name', ax=ax, palette=colors)

        ax.set_title(f'IP-Advantage: {_label.get(n, f"Top {n}")}', fontweight='bold')
        max_w = df_plot[col_adv].max() * 1.25
        ax.set_xlim(0, max_w)
        ax.set_xlabel('Advantage if Plays')
        ax.set_ylabel('')

        for i, patch in enumerate(ax.patches):
            y       = patch.get_y() + patch.get_height() / 2
            p_val   = df_plot.iloc[i][col_p]
            ip_val  = df_plot.iloc[i][col_ip]
            adv_val = df_plot.iloc[i][col_adv]
            ax.text(adv_val + 0.1, y, f'{p_val*100:.1f}% | {ip_val*100:.1f}% IP',
                    va='center', ha='left', fontsize=10, fontweight='heavy')

    plt.tight_layout()
    plt.show()


def plot_pnl_histogram(pnl, title='P&L Distribution — Kalshi TT Portfolio'):
    """Histogram + KDE of a P&L simulation array (e.g. from run_portfolio_mc_score)."""
    fig, ax = plt.subplots(figsize=(8, 6))
    sns.histplot(x=pnl, kde=True, ax=ax, color='steelblue', bins=50)
    ax.set_title(title, fontweight='bold')
    ax.set_xlabel('P&L ($)')
    ax.set_ylabel('Count')
    plt.tight_layout()
    plt.show()
    plt.close(fig)
