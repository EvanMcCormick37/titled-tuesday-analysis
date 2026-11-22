"""The VIP cohort: the top players actually traded, used to score backtests where it counts.

Names are as Kalshi labels contracts. `resolve` maps them to every chess.com account
on record for that player, through the same alias and word-set matching trading
uses (`data.match_player`). Players with no account on record are reported and
left out.
"""
from __future__ import annotations

VIP_PLAYERS: tuple[str, ...] = (
    'Nodirbek Yakubboev', 'Anton Demchenko', 'Aravindh Chithambaram', 'Magnus Carlsen', 'Hikaru Nakamura',
    'Alireza Firouzja', 'Aleksandar Indjic', 'Alexander Grischuk', 'Alexandra Kosteniuk', 'Alexey Sarana',
    'Andrew Tang', 'Andrey Esipenko', 'Andy Woodward', 'Anish Giri', 'Anna Muzychuk', 'Aram Hakobyan',
    'Arjun Erigaisi', 'Arseniy Nesterov', 'Artin Ashraf', 'Artur Ganiev', 'Awonder Liang', 'Benjamin Bok',
    'Bogdan-Daniel Deac', 'Christopher Woojin Yoo', 'Daniil Dubov', 'David Navara', 'David Paravyan',
    'Denis Lazavik', 'Ding Liren', 'Dmitry Andreikin', 'Ediz Gurel', 'Ethan Sheehan', 'Fabiano Caruana',
    'Faustino Oro', 'Felix Ilinca', 'Frederik Svane', 'Gata Kamsky', 'Grigoriy Oparin', 'Gukesh Dommaraju',
    'Haik Martirosyan', 'Hans Niemann', 'Haowen Xue', 'Ian Nepomniachtchi', 'Jakhongir Vakhidov',
    'Jan-Krzysztof Duda', 'Javokhir Sindarov', 'Jeffery Xiong', 'Jorden van Foreest',
    'Jose Eduardo Martinez Alcantara', 'Jules Moussard', 'Khuong Duy Dau', 'Le Quang Liem',
    'Leon Luke Mendonca', 'Levon Aronian', 'Martyn Kravtsiv', 'Matthias Bluebaum', 'Maxime Vachier-Lagrave',
    'Nguyen Ngoc Truong Son', 'Nihal Sarin', 'Nikita Matinian', 'Nikolay Kleimenov', 'Nils Grandelius',
    'Nodirbek Abdusattorov', 'Oleksandr Bortnyk', 'Parham Maghsoodloo', 'Pavel Eljanov', 'Peter Svidler',
    'Pouya Idani', 'Praggnanandhaa Rameshbabu', 'Pranav Venkatesh', 'Pranesh Munirethinam', 'Quoc Hy Nguyen',
    'Rasmus Svane', 'Raunak Sadhwani', 'Renato Terry Lujan', 'Reza Mahdavi', 'Richard Rapport',
    'Rohith Krishna', 'Sahib Singh', 'Sam Sevian', 'Samvel Ter-Sahakyan', 'Sergei Zhigalko',
    'Sergey Sklokin-Bagiyan', 'Seyyed Mohammad Amin Tabatabaei', 'Shakhriyar Mamedyarov', 'Sina Movahed',
    'Sunilduth Lyna Narayanan', 'Teimour Radjabov', 'Tuan Minh Le', 'Vidit Gujrathi', 'Vincent Keymer',
    'Viswanathan Anand', 'Vladimir Fedoseev', 'Vladislav Artemiev', 'Volodar Murzin', 'Wei Yi', 'Wesley So',
    'Yagiz Kaan Erdogmus', 'Yu Yangyi', 'Zhamsaran Tsydypov',
)


def resolve(conn, names: tuple[str, ...] = VIP_PLAYERS) -> tuple[dict[str, str], list[str]]:
    """(username → canonical player name for every account of every VIP, unmatched names)."""
    from .data import _PLAYER_ALIASES, match_player

    accounts: dict[str, list[str]] = {}
    for username, player_name in conn.execute(
            'SELECT username, player_name FROM player_information WHERE player_name IS NOT NULL').fetchall():
        accounts.setdefault(player_name, []).append(username)
    canonical = {p: p for p in accounts}
    out: dict[str, str] = {}
    unmatched: list[str] = []
    for name in dict.fromkeys(names):
        player = match_player(_PLAYER_ALIASES.get(name, name), canonical)
        if player is None:
            unmatched.append(name)
            continue
        for username in accounts[player]:
            out[username] = player
    return out, unmatched
