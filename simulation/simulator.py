import functools
from collections import defaultdict

import polars as pl

from datautil.load.fpl import load_fixtures
from datautil.load.fplcache import load_static_elements, load_static_teams
from datautil.load.merged import load_merged
from datautil.utils import get_mapper, get_seasons
from game.rules import DEF, ELEMENT_TYPES, FWD, GKP, MID, MNG

from .utils import (
    calculate_budget,
    calculate_points,
    calculate_team_value,
    calculate_transfer_cost,
    count_transfers,
    get_purchase_prices,
    get_selling_prices,
    load_results,
    make_automatic_substitutions,
    make_random_squad,
    remove_upcoming_data,
    update_free_transfers,
    update_purchase_prices,
)


class Simulator:
    def __init__(self, season: str):
        self.season = season
        self.first_gameweek = 1
        self.last_gameweek = 38
        self.next_gameweek = self.first_gameweek
        self.season_points = 0
        self.initialize_data()
        self.initialize_team()

    def initialize_data(self):
        """Load data, fixtures, and results for the season."""
        self._unfiltered_players, self._unfiltered_teams, self._unfiltered_managers = (
            _get_unfiltered_history(self.season)
        )
        self.fixtures = _get_fixtures(self.season)
        self.results = _get_results(self.season)

    def initialize_team(self):
        """Create a random squad for the season."""
        static_elements = _get_static_elements(self.season, self.first_gameweek)
        self.squad, self.budget = make_random_squad(static_elements)
        self.purchase_prices = get_purchase_prices(self.squad, static_elements)
        self.free_transfers = 0

    @property
    def static_elements(self):
        """Load static elements for the current gameweek."""
        return _get_static_elements(self.season, self.next_gameweek)

    @property
    def static_teams(self):
        """Load static teams for the current gameweek."""
        return _get_static_teams(self.season, self.next_gameweek)

    @property
    def static_players(self):
        """Load static players for the current gameweek."""
        return _get_static_players(self.season, self.next_gameweek)

    @property
    def static_managers(self):
        """Load static managers for the current gameweek."""
        return _get_static_managers(self.season, self.next_gameweek)

    @property
    def historical_players(self):
        """Load historical players data up to the current gameweek."""
        return _get_historical_players(self.season, self.next_gameweek)

    @property
    def historical_teams(self):
        """Load historical teams data up to the current gameweek."""
        return _get_historical_teams(self.season, self.next_gameweek)

    @property
    def historical_managers(self):
        """Load historical managers data up to the current gameweek."""
        return _get_historical_managers(self.season, self.next_gameweek)

    @property
    def selling_prices(self):
        """Calculate the selling prices for the current squad."""
        now_costs = get_mapper(self.static_elements, "id", "now_cost")
        return get_selling_prices(self.squad, self.purchase_prices, now_costs)

    def update(self, roles: dict, wildcard_gameweeks: list[int], log: bool = False):
        """Updates the squad and results for the next gameweek."""

        # Get the results of the gameweek
        gameweek_results = self.results.filter(pl.col("round") == self.next_gameweek)
        minutes = get_mapper(gameweek_results, "element", "minutes")
        total_points = get_mapper(gameweek_results, "element", "total_points")
        element_types = get_mapper(self.static_elements, "id", "element_type")
        now_costs = get_mapper(self.static_elements, "id", "now_cost")
        web_names = get_mapper(self.static_elements, "id", "web_name")

        # Calculate points scored by the squad
        substituted_roles = make_automatic_substitutions(roles, minutes, element_types)
        total_points = defaultdict(lambda: 0, total_points)
        gameweek_points = calculate_points(substituted_roles, total_points)

        # Update the budget, purchase prices, and free transfers
        new_squad = {
            *roles["starting_xi"],
            roles["reserve_gkp"],
            roles["reserve_out_1"],
            roles["reserve_out_2"],
            roles["reserve_out_3"],
        }
        transfers_made = count_transfers(self.squad, new_squad)
        transfer_cost = calculate_transfer_cost(
            self.free_transfers,
            transfers_made,
            self.next_gameweek,
            wildcard_gameweeks,
        )
        new_budget = calculate_budget(
            self.squad, new_squad, self.budget, self.selling_prices, now_costs
        )
        new_purchase_prices = update_purchase_prices(
            new_squad, self.purchase_prices, now_costs
        )
        new_selling_prices = get_selling_prices(
            new_squad, new_purchase_prices, now_costs
        )
        new_team_value = calculate_team_value(new_squad, new_selling_prices, new_budget)
        new_free_transfers = update_free_transfers(
            self.free_transfers,
            transfers_made,
            self.next_gameweek,
            wildcard_gameweeks,
        )

        # Update the overall points tally
        self.season_points += gameweek_points - transfer_cost

        if log:
            print_gameweek_summary(
                self.next_gameweek,
                gameweek_points,
                roles,
                substituted_roles,
                self.squad,
                new_squad,
                new_budget,
                new_team_value,
                transfer_cost,
                self.free_transfers,
                element_types,
                web_names,
                total_points,
                now_costs,
                self.selling_prices,
            )

        # Update the variables for the next gameweek
        self.squad = new_squad
        self.budget = new_budget
        self.purchase_prices = new_purchase_prices
        self.free_transfers = new_free_transfers

        # Move to the next gameweek
        self.next_gameweek += 1

        # Skip cancelled gameweeks.
        if self.season == "2022-23" and self.next_gameweek == 7:
            self.next_gameweek = 8


def print_gameweek_summary(
    gameweek: int,
    gameweek_points: int,
    selected_roles: dict,
    substituted_roles: dict,
    initial_squad: set,
    final_squad: set,
    final_budget: int,
    final_team_value: int,
    transfer_cost: int,
    free_transfers: int,
    element_types: dict,
    web_names: dict,
    total_points: dict,
    now_costs: dict,
    selling_prices: dict,
):
    """Prints a report of the gameweek's activity and performance."""

    element_type_names = ELEMENT_TYPES

    print(f"Gameweek {gameweek}: {gameweek_points} points")

    # Print the starting XI, including substitutions and captains
    print("Starting XI:")

    headers = ["", "", "Position", "Name", "Points"]
    data = []
    for player in sorted(substituted_roles["starting_xi"], key=element_types.get):
        row = []

        if player not in selected_roles["starting_xi"]:
            row.append("->")
        else:
            row.append("  ")

        if player == substituted_roles["captain"]:
            row.append("(C)")
        elif player == substituted_roles["vice_captain"]:
            row.append("(V)")
        else:
            row.append("   ")

        row.append(element_type_names[element_types[player]])
        row.append(web_names[player])
        row.append(total_points[player])

        data.append(row)

    print_table(data, headers)

    # Print the reserve players, including substitutions and captains
    print("Reserves:")

    headers = ["", "", "Position", "Name", "Points"]
    data = []
    for player in [
        substituted_roles["reserve_gkp"],
        substituted_roles["reserve_out_1"],
        substituted_roles["reserve_out_2"],
        substituted_roles["reserve_out_3"],
    ]:
        row = []

        if player in selected_roles["starting_xi"]:
            row.append("<-")
        else:
            row.append("  ")

        if player == selected_roles["captain"]:
            row.append("(*C)")
        elif player == selected_roles["vice_captain"]:
            row.append("(*V)")
        else:
            row.append("    ")

        row.append(element_type_names[element_types[player]])
        row.append(web_names[player])
        row.append(total_points[player])

        data.append(row)

    print_table(data, headers)

    # Print transfer activity and final budget
    print(f"Transfers ({transfer_cost} points) [Free transfers: {free_transfers}]")
    for player in set(final_squad) - set(initial_squad):
        print(f"-> {web_names[player]} ({format_currency(now_costs[player])})")
    for player in set(initial_squad) - set(final_squad):
        print(f"<- {web_names[player]} ({format_currency(selling_prices[player])})")

    print(f"Bank: {format_currency(final_budget)}")
    print(f"Team value: {format_currency(final_team_value)}")


def format_currency(amount: int):
    """Format game currency as a string."""
    return f"${round(amount / 10, 1)}"


def print_table(data, headers=None):
    """Prints a table from a list of lists."""
    if not data:
        return
    # Calculate column widths
    column_widths = [
        max(len(str(item)) for item in col)
        for col in zip(*(data + ([headers] if headers else [])), strict=False)
    ]
    # Print header
    if headers:
        print(
            " ".join(
                header.ljust(width)
                for header, width in zip(headers, column_widths, strict=False)
            )
        )
        print("-" * sum(column_widths + [len(headers) - 1]))
    # Print data rows
    for row in data:
        print(
            " ".join(
                str(item).ljust(width)
                for item, width in zip(row, column_widths, strict=False)
            )
        )


@functools.lru_cache
def _get_unfiltered_history(season: str):
    """Load unfiltered historical data for the given season."""
    seasons = get_seasons(season, 3)
    players, teams, managers = load_merged(seasons)
    return players.collect(), teams.collect(), managers.collect()


@functools.lru_cache
def _get_fixtures(season: str):
    """Load fixtures for the given season."""
    return load_fixtures([season]).collect()


@functools.lru_cache
def _get_results(season: str):
    """Load results for the given season."""
    return load_results(season).collect()


@functools.lru_cache
def _get_static_elements(season: str, gameweek: int):
    """Load static elements for the given season and gameweek."""
    return load_static_elements(season, gameweek).collect()


@functools.lru_cache
def _get_static_teams(season: str, gameweek: int):
    """Load static teams for the given season and gameweek."""
    return load_static_teams(season, gameweek).collect()


@functools.lru_cache
def _get_static_players(season: str, gameweek: int):
    """Load static players for the given season and gameweek."""
    static_elements = _get_static_elements(season, gameweek)
    return static_elements.filter(pl.col("element_type").is_in([GKP, DEF, MID, FWD]))


@functools.lru_cache
def _get_static_managers(season: str, gameweek: int):
    """Load static managers for the given season and gameweek."""
    static_elements = _get_static_elements(season, gameweek)
    return static_elements.filter(pl.col("element_type") == MNG)


@functools.lru_cache
def _get_historical_players(season: str, gameweek: int):
    """Load historical players data up to the given season and gameweek."""
    players, _, _ = _get_unfiltered_history(season)
    return remove_upcoming_data(players, season, gameweek)


@functools.lru_cache
def _get_historical_teams(season: str, gameweek: int):
    """Load historical teams data up to the given season and gameweek."""
    _, teams, _ = _get_unfiltered_history(season)
    return remove_upcoming_data(teams, season, gameweek)


@functools.lru_cache
def _get_historical_managers(season: str, gameweek: int):
    """Load historical managers data up to the given season and gameweek."""
    _, _, managers = _get_unfiltered_history(season)
    return remove_upcoming_data(managers, season, gameweek)
