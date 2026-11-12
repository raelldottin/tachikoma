"""The shipped compatibility ledger.

Every entry comes from a fact already recorded in the repository: versions and
dates are the ones the cited source records, and ``None`` where it records none.
The read-path entries cite the sanitized captures in tests/fixtures/live_sanitized/,
taken by the owner-dispatched G1 runs of 2026-11-12 (SPEC-03 S03-R7). When two repository sources disagree on how
strong a source is, the weaker class is recorded and the note says why.

See docs/baseline/compatibility-ledger.md.
"""

from __future__ import annotations

from sdk.compat.ledger import Evidence, LedgerEntry, validate_ledger

# The ClientVersion sdk/client.py sends on DeviceLogin17 and FinaliseBattle15.
# tests/test_compat_ledger.py keeps this in step with the client source.
DECLARED_CLIENT_VERSION = "0.999.59"

README = "README.template"
ROADMAP = "docs/PRODUCT_ROADMAP.md"
ACTION_CATALOG = "docs/baseline/action-catalog.md"
DIGEST = "docs/pss-pvp-research-digest.md"
FACTS = "docs/pss-pvp-research-facts.json"
CLIENT = "sdk/client.py"
SECURITY = "sdk/security.py"
CREW = "sdk/crew_leveling.py"
FORMULAS = "sdk/game_formulas.py"

LIVE_CAPTURES = "tests/fixtures/live_sanitized/README.md"
G1_EVIDENCE = "docs/experiments/live/g1-evidence.md"

HULL14_NOTES = "https://blog.pixelstarships.com/2026/06/17/hull-14-major-update-patch-notes-v0-999-55/"
PREVIEW_1_0 = "https://blog.pixelstarships.com/2026/10/16/sneak-peek-pixel-starships-1-0/"

_LIVE_BATTLE_CLAIM = Evidence(
    "dynamically-demonstrated",
    README,
    game_version=None,
    observed="2026-08-13",
    note=(
        "Battle Flow says all three battle endpoints were verified against the live server with "
        "5 accounts in CI run 31752041641 (recorded in commit 5377204). The capability inventory "
        "classes this live-claimed: documentation, not re-verified. The claim records no game "
        "version."
    ),
)

_WIKI_NOTE = (
    "Wiki formula; the module docstring calls pixelstarships.fandom.com the official wiki, but "
    "docs/pss-pvp-research-digest.md classes Fandom wiki content as community, so community is "
    "recorded. No game version is recorded."
)

_GUIDE_NOTE = (
    "From the community Crew Planning and Training Guide by Raisinbunhk (README.template Crew "
    "Stat Formulas). No game version is recorded."
)

_DIGEST_COMMUNITY_NOTE = (
    "docs/pss-pvp-research-digest.md section 4 lists official patch notes and community wikis "
    "together without attributing each fact, so community is recorded. No game version is "
    "recorded."
)


def _g1_read(entry_id: str, subject: str, note: str, *code: str) -> LedgerEntry:
    """A read endpoint seen in the G1 captures: the server settings reported RecommendedVersion 1.0."""
    return LedgerEntry(
        entry_id,
        "endpoint",
        subject,
        (
            Evidence(
                "dynamically-demonstrated",
                LIVE_CAPTURES,
                game_version="1.0",
                observed="2026-11-12",
                note=note + " Version: the server's RecommendedVersion in the same runs (the client declares "
                "0.999.59); see " + G1_EVIDENCE + ".",
            ),
        ),
        code=code,
    )


def _formula(entry_id: str, subject: str, source: str, *code: str, note: str = _WIKI_NOTE) -> LedgerEntry:
    return LedgerEntry(entry_id, "formula", subject, (Evidence("community", source, note=note),), code=code)


LEDGER: tuple[LedgerEntry, ...] = validate_ledger(
    (
        # ---------------------------------------------------------------
        # Read path (G1 captures, 2026-11-12)
        _g1_read(
            "endpoint.user.device-login17-profile",
            "The DeviceLogin17 User record holds trophies in @Trophy, starbux in @Credits and @HighestTrophy",
            "pss5/user_login.xml (owner-dispatched capture run 43187703923).",
            "sdk.state.profile_reads:parse_profile",
        ),
        _g1_read(
            "endpoint.ladder.find-user-ranking",
            "FindUserRanking returns the ladder position (@Ranking, @TotalUsers, @TotalActiveUsers) and no trophy count",
            "pss1/find_user_ranking.xml and pss5/find_user_ranking.xml (run 43187703923).",
            "sdk.client:Client.findUserRanking",
            "sdk.state.profile_reads:parse_ranking",
        ),
        _g1_read(
            "endpoint.battle.list-pvp-battles2",
            "ListPvPBattles2 lists Battle records under ListPvPBattles/Battles with BattleId, Attacking/DefendingUserId, "
            "OutcomeType and Win/LoseTrophyResult; a new account's list is empty",
            "pss1/list_pvp_battles2.xml (10 battles) and pss5/list_pvp_battles2.xml (none), run 43187703923. "
            "Trophy counts in the records were all 0, so they are read as unreported.",
            "sdk.client:Client.listPvPBattles2",
            "sdk.state.profile_reads:parse_battles",
        ),
        _g1_read(
            "endpoint.ship.get-ship-by-user-id-empty-containers",
            "GetShipByUserId on a new ship returns empty <Rooms/> and <Researches/> elements",
            "Every ship read of pss2 to pss5 failed on these in the first capture run; fixed in #183 and read "
            "cleanly in run 43187703923.",
            "sdk.client:Client.getShipByUserId",
        ),
        _g1_read(
            "endpoint.setting.get-latest-version3-fields",
            "GetLatestVersion3's Setting has no LatestVersion, LatestClientVersion or LatestVersionIOS; it reports "
            "RecommendedVersion, MinimumClientVersion, MinimumVersion and CurrentAndroidVersion, plus integer "
            "data counters such as ServerSettingVersion",
            "pss1/server_settings_fields.json (field names only), run 43187703923; read by #187.",
            "sdk.fleet.factory:server_game_version",
        ),
        # ---------------------------------------------------------------
        # Session and battle chain
        # ---------------------------------------------------------------
        LedgerEntry(
            "rule.client.declared-version",
            "rule",
            f"The client identifies as ClientVersion {DECLARED_CLIENT_VERSION} (ClientBuild 18054) "
            "in DeviceLogin17 and sends the same clientVersion to FinaliseBattle15",
            (
                Evidence(
                    "unverified",
                    CLIENT,
                    game_version=DECLARED_CLIENT_VERSION,
                    note=(
                        "The version the client declares, not one the server reported. Whether "
                        "the server still accepts it is not recorded."
                    ),
                ),
            ),
            code=("sdk.client:Client.finaliseBattle15",),
        ),
        LedgerEntry(
            "endpoint.user.heartbeat4",
            "endpoint",
            "HeartBeat4 keeps the session alive; checksum is the numeric sum "
            "ChecksumTimeForDate(ticks) + ChecksumPasswordWithString(accessToken)",
            (
                Evidence(
                    "dynamically-demonstrated",
                    SECURITY,
                    observed="2026-08-10",
                    note=(
                        "checksum_heartbeat4 docstring: matched against 267 mitmproxy captures "
                        "taken 2026-08-08 to 2026-08-10. The captures record no game version."
                    ),
                ),
            ),
            code=("sdk.client:Client.heartbeat", "sdk.security:checksum_heartbeat4"),
        ),
        LedgerEntry(
            "endpoint.battle.create-battle9",
            "endpoint",
            "CreateBattle9 request: checksum = MD5(clientDateTime + checksum_key + savy_checksum), "
            "parameters sent in the query and as a form body",
            (
                Evidence(
                    "dynamically-demonstrated",
                    CLIENT,
                    note="createBattle9 docstring: checksum verified against a capture. No version or date recorded.",
                ),
                _LIVE_BATTLE_CLAIM,
            ),
            code=("sdk.client:Client.createBattle9",),
        ),
        LedgerEntry(
            "endpoint.battle.create-battle9-response",
            "endpoint",
            "CreateBattle9 responses wrap the battle in <CreateBattle>, not <CreateBattle9>",
            (
                Evidence(
                    "dynamically-demonstrated",
                    CLIENT,
                    observed="2026-08-12",
                    note=(
                        "createBattle9 comment: verified against live CI run 31656147771 "
                        "(recorded in commit dd0b91b). No game version recorded."
                    ),
                ),
            ),
            code=("sdk.client:Client.createBattle9",),
        ),
        LedgerEntry(
            "endpoint.battle.accept-battle5",
            "endpoint",
            "AcceptBattle5 request: checksum = MD5(accessToken + battleId + clientDateTime + "
            "savy_checksum); itemDesignId and checksum_key are not in the preimage",
            (
                Evidence(
                    "dynamically-demonstrated",
                    CLIENT,
                    note="acceptBattle5 docstring: verified against 3 mitmproxy captures. No version or date recorded.",
                ),
                _LIVE_BATTLE_CLAIM,
            ),
            code=("sdk.client:Client.acceptBattle5",),
        ),
        LedgerEntry(
            "endpoint.battle.finalise-battle15",
            "endpoint",
            "FinaliseBattle15 request: checksum = MD5(battleId + clientOutcomeType + clientEndFrame "
            "+ clientResultString + attackingShipHp + clientVersion + accessToken + checksum_key "
            "+ savy_checksum)",
            (
                Evidence(
                    "static-analysis",
                    CLIENT,
                    note="finaliseBattle15 comment: formula from static analysis of IL2CPP metadata.",
                ),
                _LIVE_BATTLE_CLAIM,
            ),
            code=("sdk.client:Client.finaliseBattle15",),
        ),
        LedgerEntry(
            "rule.battle.outcome-observed",
            "rule",
            "A live battle's server outcome (win, loss, trophy change) can be observed after "
            "FinaliseBattle15",
            (
                Evidence(
                    "unverified",
                    ACTION_CATALOG,
                    note=(
                        "Battle evidence tiers: a finalise response is never evidence of a win, "
                        "and live outcomes stay unknown without a trophy read. "
                        "docs/PRODUCT_ROADMAP.md section 9 asks for request shape, acceptance, "
                        "finalization, server outcome and competitive performance to be "
                        "classified separately."
                    ),
                ),
            ),
            code=("sdk.client:Client.runBattleEndToEnd",),
        ),
        # ---------------------------------------------------------------
        # Marketplace path
        # ---------------------------------------------------------------
        LedgerEntry(
            "endpoint.market.list-active-marketplace-messages5",
            "endpoint",
            "MessageService/ListActiveMarketplaceMessages5 lists active marketplace sales",
            (
                Evidence(
                    "unverified",
                    ROADMAP,
                    note=(
                        "Section 9: identified by prior operator-provided capture analysis that is "
                        "not in the repository; treat as historical evidence to reconcile, not "
                        "proof the endpoint is current."
                    ),
                ),
            ),
            code=("sdk.client:Client.listActiveMarketplaceMessages",),
        ),
        LedgerEntry(
            "endpoint.market.sell-item3",
            "endpoint",
            "SellItem3 lists an item for sale",
            (
                Evidence(
                    "unverified",
                    ROADMAP,
                    note=(
                        "Section 9 operator capture analysis only. Not implemented as a Client "
                        "method; the R4 live market trial is blocked on sanitized current captures."
                    ),
                ),
            ),
        ),
        LedgerEntry(
            "endpoint.market.purchase4",
            "endpoint",
            "Purchase4 buys a listed sale by its exact SaleId",
            (
                Evidence(
                    "unverified",
                    ROADMAP,
                    note=(
                        "Section 9 operator capture analysis only. Not implemented as a Client "
                        "method; no reconciled trade has been recorded."
                    ),
                ),
            ),
        ),
        LedgerEntry(
            "endpoint.market.gift-item",
            "endpoint",
            "A live GiftItem route for moving items between accounts",
            (
                Evidence(
                    "unverified",
                    ROADMAP,
                    note=(
                        "Section 9: the capture analysis did not establish a live GiftItem route "
                        "or a reconciled reciprocal trade. docs/pss-pvp-research-facts.json lists "
                        "item transferability between accounts as unverified."
                    ),
                ),
            ),
        ),
        # ---------------------------------------------------------------
        # Official releases and announcements
        # ---------------------------------------------------------------
        LedgerEntry(
            "rule.hull.max-level",
            "rule",
            "Maximum hull (ship) level is 14",
            (
                Evidence(
                    "official-release",
                    HULL14_NOTES,
                    game_version="0.999.55",
                    observed="2026-06-17",
                    note="Hull 14 patch notes, as recorded in docs/pss-pvp-research-facts.json (max_hull).",
                ),
            ),
        ),
        LedgerEntry(
            "rule.crew.cloak-movement-halved",
            "rule",
            "Cloaked crew move at half speed",
            (
                Evidence(
                    "official-release",
                    DIGEST,
                    game_version="0.999.55",
                    observed="2026-06-17",
                    note=(
                        "Digest section 5, consolidated from official patch notes, lists the cloak "
                        "nerf; section 4.9 attributes the halved movement to Hull 14 (V0.999.55)."
                    ),
                ),
            ),
        ),
        LedgerEntry(
            "rule.room.shield-reload",
            "rule",
            "Shield rooms reload in 7 seconds (was 8)",
            (
                Evidence(
                    "official-release",
                    DIGEST,
                    game_version="0.999.58",
                    note="Digest section 5, consolidated from official patch notes, tags the change V0.999.58.",
                ),
            ),
        ),
        LedgerEntry(
            "rule.research.room-reinforcement",
            "rule",
            "Room Reinforcement is a new research requirement",
            (
                Evidence(
                    "official-release",
                    DIGEST,
                    game_version="0.999.60",
                    observed="2026-08-17",
                    note=(
                        "Digest sections 4.6, 4.7 and 12: Galaxy patch notes V0.999.60, the latest "
                        "official patch the digest reviewed (docs/pss-pvp-research-facts.json)."
                    ),
                ),
            ),
        ),
        LedgerEntry(
            "rule.release.pixel-starships-1-0",
            "rule",
            "Pixel Starships 1.0 has been announced",
            (
                Evidence(
                    "official-announcement",
                    PREVIEW_1_0,
                    observed="2026-10-16",
                    note=(
                        "Preview cited in docs/PRODUCT_ROADMAP.md section 9. The repository records "
                        "no release version number or mechanics from it."
                    ),
                ),
            ),
        ),
        # ---------------------------------------------------------------
        # Community battle rules
        # ---------------------------------------------------------------
        LedgerEntry(
            "rule.pvp.battle-duration",
            "rule",
            "A PvP battle lasts 180 seconds",
            (Evidence("community", FACTS, note=_DIGEST_COMMUNITY_NOTE),),
        ),
        LedgerEntry(
            "rule.pvp.defense-immunity",
            "rule",
            "Immunity lasts 8 hours after a failed defense and 20 minutes after a successful one",
            (Evidence("community", FACTS, note=_DIGEST_COMMUNITY_NOTE),),
        ),
        LedgerEntry(
            "rule.pvp.matchmaking-range",
            "rule",
            "PvP matches against an offline opponent of roughly plus or minus 2 ship levels, by trophies",
            (Evidence("community", DIGEST, note=_DIGEST_COMMUNITY_NOTE),),
        ),
        LedgerEntry(
            "rule.room.non-powered-armor",
            "rule",
            "Non-powered rooms have 300% base armor",
            (Evidence("community", FACTS, note=_DIGEST_COMMUNITY_NOTE),),
        ),
        # ---------------------------------------------------------------
        # Formulas
        # ---------------------------------------------------------------
        _formula(
            "formula.crew.level-cap",
            "Crew level cap = ship level x 4, max 40",
            CREW,
            "sdk.crew_leveling:get_crew_level_cap",
            note=_GUIDE_NOTE,
        ),
        _formula(
            "formula.crew.tp-caps",
            "Training point caps: 3*=70, 4*=80 (Mistycball, Huge Hellaluya 100), 5*=90, "
            "6*=100 (captains 200), 7*=110",
            CREW,
            "sdk.crew_leveling:get_tp_cap",
            note=_GUIDE_NOTE,
        ),
        _formula(
            "formula.crew.stat-buff",
            "Final stat = base x (1 + TP/100) + equipment; ability x (1 + equipment%); "
            "stamina = TP + equipment",
            CREW,
            "sdk.crew_leveling:compute_final_stat",
            "sdk.crew_leveling:compute_final_ability",
            "sdk.crew_leveling:compute_final_stamina",
            note=_GUIDE_NOTE,
        ),
        _formula(
            "formula.room.reload",
            "Boosted reload = base / ((100 + crew stat) / 100), then adjusted for power",
            FORMULAS,
            "sdk.game_formulas:room_reload_boosted",
            "sdk.game_formulas:room_reload_powered",
            "sdk.game_formulas:room_reload",
        ),
        _formula(
            "formula.escape",
            "Escape chance = mod x (100 + pilot stat) / 100; escape rate rounded to 5%",
            FORMULAS,
            "sdk.game_formulas:escape_chance",
            "sdk.game_formulas:escape_rate",
        ),
        _formula(
            "formula.dodge",
            "Evasion = 100 x (1 - exp(combined/100 x ln 0.2)), capped at 80%",
            FORMULAS,
            "sdk.game_formulas:dodge_evasion",
            note=_WIKI_NOTE + " The module labels it the post-July 2023 formula.",
        ),
        _formula(
            "formula.damage-reduction",
            "Damage reduction = 100 x (1 - 100 / (100 + armor))",
            FORMULAS,
            "sdk.game_formulas:damage_reduction",
            "sdk.game_formulas:effective_damage",
        ),
        _formula(
            "formula.fire-damage",
            "Fire damage reduced by sprinklers; crew and AP damage from fire duration",
            FORMULAS,
            "sdk.game_formulas:fire_damage_reduced",
            "sdk.game_formulas:fire_crew_damage",
            "sdk.game_formulas:fire_ap_damage",
        ),
        _formula(
            "formula.crew.stat-by-level",
            "Crew stat at level = lvl1 + (max - lvl1) x ((level - 1) / 39) ^ ease",
            FORMULAS,
            "sdk.game_formulas:crew_stat_at_level",
        ),
        _formula(
            "formula.gas-draw-price",
            "Gas draw price, capped at 2,000,000",
            FORMULAS,
            "sdk.game_formulas:gas_draw_price",
        ),
        _formula(
            "formula.pvp.trophy-gain",
            "Trophies gained by a PvP winner, capped 1 to 40",
            FORMULAS,
            "sdk.game_formulas:trophy_gain",
        ),
    )
)
