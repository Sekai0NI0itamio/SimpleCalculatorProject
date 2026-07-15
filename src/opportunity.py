#!/usr/bin/env python3
"""
Opportunity Engine — Decision Engine for Mod Market Analysis

Turns market data into actionable recommendations: "what mod to build next"
with confidence, by version + loader + niche.

Phase 1: Concept clustering from mod titles/descriptions
Phase 2: Demand-vs-supply opportunity scoring
Phase 3: Version+loader recommendation engine

Output: ranked opportunities with reason codes, target audience,
recommended game versions, recommended loaders, competition level,
expected growth window, confidence and risk flags.
"""

import math
import json
import re
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter

# =============================================================================
# CONCEPT CLUSTER DEFINITIONS
# =============================================================================

# Each cluster maps Modrinth categories + keyword patterns to a concept.
# Keywords are matched against project title + description (case-insensitive).
# Categories are exact matches against project.categories.
CONCEPT_CLUSTERS = {
    "performance": {
        "label": "Performance & Optimization",
        "categories": ["optimization"],
        "keywords": [
            "performance", "optimization", "optimize", "fps", "lag", "sodium",
            "optifine", "fast", "faster", "smooth", "boost", "speed", "render",
            "rendering", "chunk", "memory", "cull", "culling", "stutter",
            "frame", "framerate", "tick", "latency", "graphics", "gpu",
            "vsync", "benchmark", "lighting", "shader", "tesselation",
        ],
        "description": "Performance optimization, FPS improvements, lag fixes, rendering enhancements",
        "target_audience": "All players (universal appeal)",
    },
    "magic": {
        "label": "Magic & Fantasy",
        "categories": ["magic"],
        "keywords": [
            "magic", "spell", "wizard", "sorcery", "mana", "enchant", "enchanting",
            "rune", "arcane", "mystic", "ritual", "alchemy", "elemental",
            "potion", "wand", "staff", "occult", "necromancy", "druid",
            "witch", "warlock", "sorcerer", "magical", "mystical",
        ],
        "description": "Magic systems, spells, enchantments, mystical content",
        "target_audience": "Fantasy/RPG players",
    },
    "tech": {
        "label": "Technology & Automation",
        "categories": ["technology"],
        "keywords": [
            "tech", "technology", "machine", "automation", "automate", "pipe",
            "power", "energy", "electric", "generator", "factory", "industrial",
            "mechanism", "mechanical", "circuit", "processor", "cable", "wire",
            "conveyor", "quarry", "drill", "miner", "furnace", "smelt",
            "redstone", "logic", "computer", "digital", "robot", "drone",
        ],
        "description": "Technology mods, machines, automation, power systems",
        "target_audience": "Tech/engineering players",
    },
    "adventure": {
        "label": "Adventure & Exploration",
        "categories": ["adventure"],
        "keywords": [
            "adventure", "explore", "exploration", "dungeon", "dungeons",
            "boss", "bosses", "structure", "temple", "ruin", "quest",
            "dimension", "portal", "biome", "cave", "cavern", "underground",
            "sky", "floating", "lost", "ancient", "treasure", "loot",
            "discovery", "discover", "mystery", "hidden", "secret",
        ],
        "description": "Adventure mods, exploration, dungeons, bosses, new dimensions",
        "target_audience": "Explorers and adventurers",
    },
    "qol": {
        "label": "Quality of Life",
        "categories": ["game-mechanics", "management"],
        "keywords": [
            "qol", "quality of life", "tweak", "fix", "utility", "convenience",
            "improve", "improvement", "easier", "easy", "simple", "simplify",
            "tooltip", "hud", "gui", "interface", "inventory", "sort",
            "sorting", "auto", "automatic", "quick", "shortcut", "hotkey",
            "config", "configure", "setting", "toggle", "widget", "overlay",
            "notification", "alert", "reminder", "timer", "coordinate",
            "minimap", "waypoint", "map", "jei", "rei", "emi", "recipe",
        ],
        "description": "Quality of life improvements, UI enhancements, convenience features",
        "target_audience": "All players (universal appeal)",
    },
    "storage": {
        "label": "Storage & Inventory",
        "categories": ["storage"],
        "keywords": [
            "storage", "inventory", "chest", "barrel", "backpack", "bag",
            "pouch", "vault", "warehouse", "organize", "organization", "sort",
            "filter", "item", "stack", "container", "drawer", "shelf",
            "cabinet", "crate", "box", "compartment", "deposit", "withdraw",
        ],
        "description": "Storage solutions, inventory management, item organization",
        "target_audience": "Builders, collectors, hoarders",
    },
    "farming": {
        "label": "Farming & Food",
        "categories": ["food"],
        "keywords": [
            "farm", "farming", "crop", "agriculture", "food", "cooking", "cook",
            "recipe", "ingredient", "meal", "dish", "fruit", "vegetable",
            "plant", "seed", "harvest", "grow", "growth", "animal", "breed",
            "breeding", "livestock", "fish", "fishing", "cattle", "chicken",
            "sheep", "pig", "cow", "bee", "honey", "apiary", "orchard",
        ],
        "description": "Farming, agriculture, food, cooking, animal husbandry",
        "target_audience": "Farmers and survival players",
    },
    "combat": {
        "label": "Combat & Weapons",
        "categories": ["equipment"],
        "keywords": [
            "combat", "weapon", "sword", "bow", "armor", "armour", "shield",
            "fight", "battle", "war", "pvp", "pve", "damage", "attack",
            "defense", "defence", "gun", "bullet", "arrow", "projectile",
            "explosive", "bomb", "tnt", "cannon", "turret", "sentry",
            "soldier", "knight", "warrior", "berserk", "ranger", "archer",
        ],
        "description": "Combat systems, weapons, armor, PvP/PvE enhancements",
        "target_audience": "Combat/PvP players",
    },
    "decoration": {
        "label": "Decoration & Building",
        "categories": ["decoration", "cursed"],
        "keywords": [
            "decor", "decoration", "decorative", "building", "build", "block",
            "furniture", "furnishing", "aesthetic", "cosmetic", "design",
            "architecture", "structure", "house", "home", "wall", "floor",
            "roof", "door", "window", "lamp", "light", "chair", "table",
            "carpet", "painting", "statue", "monument", "garden", "landscape",
            "terrain", "path", "road", "bridge", "fence", "gate", "pillar",
        ],
        "description": "Building blocks, furniture, decorative items, aesthetic enhancements",
        "target_audience": "Builders and designers",
    },
    "worldgen": {
        "label": "World Generation",
        "categories": ["world-generation"],
        "keywords": [
            "world", "generation", "worldgen", "terrain", "biome", "landscape",
            "cave", "cavern", "mountain", "ocean", "river", "forest", "desert",
            "structure", "village", "generate", "generator", "custom", "seed",
            "ore", "mineral", "resource", "geological", "geology", "climate",
        ],
        "description": "World generation, new biomes, terrain, structures, ores",
        "target_audience": "Explorers and world-builders",
    },
    "utility": {
        "label": "Utilities & Libraries",
        "categories": ["utility", "library"],
        "keywords": [
            "library", "api", "utility", "util", "dependency", "core", "base",
            "helper", "framework", "tool", "modding", "compatibility", "patch",
            "fix", "bugfix", "loader", "fabric", "forge", "neoforge", "quilt",
            "server", "client", "side", "network", "sync", "packet",
        ],
        "description": "Libraries, APIs, utilities, modding tools, dependencies",
        "target_audience": "Mod developers and pack makers",
    },
    "social": {
        "label": "Social & Multiplayer",
        "categories": ["social"],
        "keywords": [
            "social", "multiplayer", "chat", "voice", "friend", "party",
            "team", "guild", "clan", "group", "community", "trade", "shop",
            "economy", "market", "currency", "money", "coin", "bank",
            "auction", "claim", "protect", "grief", "town", "city", "nation",
            "roleplay", "rp", "rank", "permission", "proximity",
        ],
        "description": "Multiplayer features, social tools, economy, claims, chat",
        "target_audience": "Server owners and multiplayer players",
    },
    "mobs": {
        "label": "Mobs & Creatures",
        "categories": ["mobs"],
        "keywords": [
            "mob", "mobs", "creature", "entity", "monster", "animal", "dragon",
            "boss", "minion", "pet", "companion", "tame", "taming", "spawn",
            "spawning", "beast", "wildlife", "dinosaur", "mythical", "legendary",
            "pokemon", "pixelmon", "evolution", "evolve", "capture", "catch",
        ],
        "description": "New mobs, creatures, pets, entity systems",
        "target_audience": "Adventurers and collectors",
    },
    "transportation": {
        "label": "Transportation & Travel",
        "categories": ["transportation"],
        "keywords": [
            "transport", "transportation", "travel", "vehicle", "car", "train",
            "rail", "airplane", "plane", "ship", "boat", "submarine", "rocket",
            "teleport", "teleportation", "warp", "portal", "waypoint", "flight",
            "fly", "flying", "jetpack", "elevator", "escalator", "horse",
            "mount", "riding", "ride", "speed", "fast travel", "grapple",
            "grappling", "hook", "zipline", "glide", "glider", "parachute",
        ],
        "description": "Transportation, vehicles, teleportation, fast travel",
        "target_audience": "Explorers and builders",
    },
    "minigame": {
        "label": "Minigames & Challenges",
        "categories": ["minigame"],
        "keywords": [
            "minigame", "mini game", "game", "challenge", "parkour", "puzzle",
            "race", "racing", "arena", "survival", "skyblock", "bedwars",
            "hunger games", "battle royale", "deathrun", "dropper", "spleef",
            "capture", "flag", "ctf", "team", "competitive", "score", "leaderboard",
            "achievement", "trophy", "reward", "level", "progression", "skill",
            "rpg", "class", "profession", "quest", "mission", "objective",
        ],
        "description": "Minigames, challenges, game modes, progression systems",
        "target_audience": "Competitive and casual players",
    },
}

# Combine all keywords from all clusters for efficient lookup
_CLUSTER_KEYWORD_MAP = {}
for cluster_id, cluster_def in CONCEPT_CLUSTERS.items():
    for kw in cluster_def["keywords"]:
        _CLUSTER_KEYWORD_MAP.setdefault(kw.lower(), []).append(cluster_id)

# Category-to-cluster mapping
_CATEGORY_CLUSTER_MAP = {}
for cluster_id, cluster_def in CONCEPT_CLUSTERS.items():
    for cat in cluster_def["categories"]:
        _CATEGORY_CLUSTER_MAP[cat] = cluster_id


# =============================================================================
# CONCEPT CLUSTERING
# =============================================================================


def extract_concepts(title, description, categories):
    """Extract concept clusters from a project's title, description, and categories.

    Uses a multi-signal approach:
    1. Category matching (strongest signal — Modrinth categories are curated)
    2. Keyword frequency in title (high weight)
    3. Keyword frequency in description (lower weight)

    Returns a list of (cluster_id, confidence) tuples sorted by confidence descending.
    """
    title_lower = (title or "").lower()
    desc_lower = (description or "").lower()
    cats = set(categories or [])

    scores = defaultdict(float)

    # Signal 1: Category matching (weight: 3.0 per category match)
    for cat in cats:
        cluster_id = _CATEGORY_CLUSTER_MAP.get(cat)
        if cluster_id:
            scores[cluster_id] += 3.0

    # Signal 2: Keyword matching in title (weight: 2.0 per keyword match)
    title_words = set(re.findall(r'\w+', title_lower))
    for word in title_words:
        for cluster_id in _CLUSTER_KEYWORD_MAP.get(word, []):
            scores[cluster_id] += 2.0

    # Signal 3: Keyword matching in description (weight: 0.5 per keyword occurrence)
    desc_words = re.findall(r'\w+', desc_lower)
    desc_word_counts = Counter(desc_words)
    for word, count in desc_word_counts.items():
        for cluster_id in _CLUSTER_KEYWORD_MAP.get(word, []):
            scores[cluster_id] += min(count, 5) * 0.5  # Cap at 5 occurrences

    # Normalize scores to 0-100 confidence
    if not scores:
        return []

    max_score = max(scores.values())
    results = []
    for cluster_id, score in scores.items():
        confidence = min(100.0, (score / max(10.0, max_score)) * 100.0)
        if confidence >= 10.0:  # Minimum confidence threshold
            results.append((cluster_id, round(confidence, 1)))

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:3]  # Top 3 clusters per project


# =============================================================================
# DEMAND-VS-SUPPLY OPPORTUNITY SCORING
# =============================================================================


def compute_demand_score(cluster_projects, baseline_map, hours_between, current_snapshot):
    """Compute demand signals for a concept cluster.

    Demand signals:
    - Growth velocity: total downloads gained per hour (normalized 0-100)
    - Growth acceleration: rate of velocity change (if multiple snapshots available)
    - Follow/download ratio: engagement proxy (higher = more engaged audience)
    - Version spread: number of game versions supported by cluster projects
    - Project freshness: new projects with high growth score higher
    - Category market share: share of total downloads

    Returns (score, detail_dict) where score is 0-100.
    """
    if not cluster_projects:
        return 0.0, {"reason": "no_projects"}

    total_downloads = sum(p.get("downloads", 0) for p in cluster_projects)
    total_follows = sum(p.get("follows", 0) for p in cluster_projects)

    # 1. Growth velocity (30%)
    total_delta = 0.0
    for p in cluster_projects:
        pid = p.get("project_id", "")
        baseline = baseline_map.get(pid, 0)
        current = p.get("downloads", 0)
        if current > baseline:
            total_delta += current - baseline

    velocity = total_delta / max(hours_between, 1)
    # Normalize: typical velocity ranges from 0 to 10000/hr for large clusters
    velocity_norm = min(100.0, (velocity / 100.0) * 100.0) if velocity > 0 else 0.0

    # 2. Follow/download ratio — engagement proxy (15%)
    if total_downloads > 0:
        follow_ratio = total_follows / total_downloads
        # Typical follow ratio: 0.001 to 0.05 (0.1% to 5%)
        follow_norm = min(100.0, (follow_ratio / 0.02) * 100.0)
    else:
        follow_norm = 0.0

    # 3. Version compatibility spread (10%)
    # How many different game versions do projects in this cluster support?
    all_game_versions = set()
    for p in cluster_projects:
        versions = p.get("_versions", [])
        for v in versions:
            for gv in v.get("game_versions", []):
                all_game_versions.add(gv)
    version_count = len(all_game_versions)
    version_norm = min(100.0, (version_count / 10.0) * 100.0)

    # 4. Project freshness (15%)
    # Newer projects with high growth = hot trend
    now = datetime.now(timezone.utc)
    fresh_score = 0.0
    fresh_count = 0
    for p in cluster_projects:
        created = p.get("date_created", "")
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_days = max(1, (now - created_dt).days)
                pid = p.get("project_id", "")
                baseline = baseline_map.get(pid, 0)
                current = p.get("downloads", 0)
                delta = current - baseline
                if delta > 0 and age_days < 365:
                    # Newer projects with high growth rate score higher
                    daily_growth = delta / max(hours_between / 24, 1)
                    fresh_score += min(100.0, daily_growth / max(age_days, 1) * 100)
                    fresh_count += 1
            except (ValueError, TypeError):
                pass
    fresh_norm = (fresh_score / max(fresh_count, 1)) if fresh_count > 0 else 0.0

    # 5. Category market growth (15%)
    # How fast is the overall category growing?
    total_all_downloads = current_snapshot.get("total_downloads", 1)
    market_share = (total_downloads / total_all_downloads * 100) if total_all_downloads > 0 else 0
    market_norm = min(100.0, market_share * 10.0)  # 10% share = 100

    # 6. Project count growth (15%)
    # Is the number of projects in this cluster growing?
    new_project_count = sum(1 for p in cluster_projects if p.get("project_id", "") not in baseline_map)
    project_count = len(cluster_projects)
    new_project_rate = (new_project_count / max(project_count, 1)) * 100
    new_project_norm = min(100.0, new_project_rate * 10.0)

    # Weighted composite
    demand_score = (
        velocity_norm * 0.30 +
        follow_norm * 0.15 +
        version_norm * 0.10 +
        fresh_norm * 0.15 +
        market_norm * 0.15 +
        new_project_norm * 0.15
    )

    return round(demand_score, 1), {
        "velocity": round(velocity, 1),
        "velocity_norm": round(velocity_norm, 1),
        "follow_ratio": round(follow_ratio, 6),
        "follow_norm": round(follow_norm, 1),
        "version_count": version_count,
        "version_norm": round(version_norm, 1),
        "fresh_norm": round(fresh_norm, 1),
        "market_share": round(market_share, 2),
        "market_norm": round(market_norm, 1),
        "new_project_rate": round(new_project_rate, 2),
        "new_project_norm": round(new_project_norm, 1),
        "total_delta": int(total_delta),
        "total_downloads": total_downloads,
        "total_follows": total_follows,
        "project_count": project_count,
        "new_project_count": new_project_count,
    }


def compute_supply_score(cluster_projects, baseline_map):
    """Compute supply (competition) signals for a concept cluster.

    Supply signals:
    - Competitor count: number of projects in cluster (normalized)
    - Market concentration: HHI within cluster (higher = dominated by few)
    - Top project dominance: CR4 within cluster
    - Average project maturity: older projects = entrenched
    - Release velocity: how active are competitors

    Returns (score, detail_dict) where score is 0-100 (higher = more competitive).
    """
    if not cluster_projects:
        return 0.0, {"reason": "no_projects"}

    downloads_list = [p.get("downloads", 0) for p in cluster_projects]
    total_dl = sum(downloads_list)
    n = len(downloads_list)

    # 1. Competitor count (30%)
    # More projects = more competition. Log scale because 10 vs 100 projects
    # is a bigger difference than 1000 vs 1100.
    competitor_norm = min(100.0, math.log(1 + n) / math.log(1000) * 100.0)

    # 2. Market concentration — HHI within cluster (20%)
    if total_dl > 0:
        hhi = sum((d / total_dl * 100) ** 2 for d in downloads_list)
        # HHI ranges 0-10000, normalize to 0-100
        hhi_norm = min(100.0, hhi / 100.0)
    else:
        hhi = 0.0
        hhi_norm = 0.0

    # 3. Top project dominance — CR4 (15%)
    if total_dl > 0:
        sorted_dl = sorted(downloads_list, reverse=True)
        cr4 = sum(sorted_dl[:4]) / total_dl * 100
        cr4_norm = min(100.0, cr4)
    else:
        cr4 = 0.0
        cr4_norm = 0.0

    # 4. Average project maturity (20%)
    # Older projects = more entrenched competition
    now = datetime.now(timezone.utc)
    ages = []
    for p in cluster_projects:
        created = p.get("date_created", "")
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_days = max(1, (now - created_dt).days)
                ages.append(age_days)
            except (ValueError, TypeError):
                pass
    if ages:
        avg_age = sum(ages) / len(ages)
        # 3+ years = very mature, 1 year = moderate, <6 months = fresh
        age_norm = min(100.0, (avg_age / 1095.0) * 100.0)  # 1095 days = 3 years
    else:
        avg_age = 365
        age_norm = 50.0

    # 5. Release velocity — how active are competitors (15%)
    # Use date_modified recency as proxy
    active_count = 0
    for p in cluster_projects:
        modified = p.get("date_modified", "")
        if modified:
            try:
                modified_dt = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                days_since_update = max(0, (now - modified_dt).days)
                if days_since_update < 90:  # Updated in last 90 days = active
                    active_count += 1
            except (ValueError, TypeError):
                pass
    activity_rate = (active_count / max(n, 1)) * 100
    activity_norm = min(100.0, activity_rate * 2.0)  # 50% active = 100

    # Weighted composite
    supply_score = (
        competitor_norm * 0.30 +
        hhi_norm * 0.20 +
        cr4_norm * 0.15 +
        age_norm * 0.20 +
        activity_norm * 0.15
    )

    return round(supply_score, 1), {
        "competitor_count": n,
        "competitor_norm": round(competitor_norm, 1),
        "hhi": round(hhi, 2),
        "hhi_norm": round(hhi_norm, 1),
        "cr4": round(cr4, 1),
        "cr4_norm": round(cr4_norm, 1),
        "avg_age_days": round(avg_age, 0),
        "age_norm": round(age_norm, 1),
        "active_count": active_count,
        "activity_rate": round(activity_rate, 1),
        "activity_norm": round(activity_norm, 1),
    }


def compute_opportunity_score(demand_score, supply_score):
    """Compute the final opportunity score.

    Opportunity = demand_score * (1 - supply_score / 100) * boost_factor

    - High demand + low supply = high opportunity
    - Low demand + high supply = low opportunity
    - Normalized to 0-100
    """
    supply_penalty = max(0.0, 1.0 - supply_score / 100.0)
    opportunity = demand_score * supply_penalty
    return round(opportunity, 1)


def compute_confidence(demand_detail, supply_detail, cluster_projects):
    """Compute confidence level for an opportunity recommendation.

    Confidence levels:
    - "high": strong signals, large sample size, consistent trends
    - "medium": moderate signals, decent sample, some consistency
    - "low": weak signals, small sample, erratic trends

    Returns (level, score) where level is "high"/"medium"/"low" and score is 0-100.
    """
    n = len(cluster_projects)
    confidence_score = 0.0

    # Sample size factor
    if n >= 200:
        confidence_score += 30
    elif n >= 50:
        confidence_score += 20
    elif n >= 20:
        confidence_score += 10
    else:
        confidence_score += 5

    # Growth signal strength
    total_delta = demand_detail.get("total_delta", 0)
    if total_delta > 10000:
        confidence_score += 30
    elif total_delta > 1000:
        confidence_score += 20
    elif total_delta > 100:
        confidence_score += 10
    else:
        confidence_score += 5

    # Market data quality
    follow_ratio = demand_detail.get("follow_ratio", 0)
    if follow_ratio > 0.001:
        confidence_score += 20
    elif follow_ratio > 0.0001:
        confidence_score += 10

    # Project age diversity
    if demand_detail.get("version_count", 0) >= 5:
        confidence_score += 20
    elif demand_detail.get("version_count", 0) >= 2:
        confidence_score += 10

    if confidence_score >= 70:
        level = "high"
    elif confidence_score >= 40:
        level = "medium"
    else:
        level = "low"

    return level, round(confidence_score, 1)


def compute_risk_flags(demand_detail, supply_detail):
    """Identify risk flags for an opportunity.

    Returns a list of risk flag objects with type, severity, and message.
    """
    flags = []

    # Crowded market
    if supply_detail.get("competitor_count", 0) > 500:
        flags.append({
            "type": "crowded_market",
            "severity": "high",
            "message": f"Highly crowded with {supply_detail['competitor_count']} competitors"
        })
    elif supply_detail.get("competitor_count", 0) > 200:
        flags.append({
            "type": "crowded_market",
            "severity": "medium",
            "message": f"Moderately crowded with {supply_detail['competitor_count']} competitors"
        })

    # Dominated market
    cr4 = supply_detail.get("cr4", 0)
    if cr4 > 80:
        flags.append({
            "type": "dominated_market",
            "severity": "high",
            "message": f"Top 4 projects control {cr4:.0f}% of downloads — hard to break in"
        })
    elif cr4 > 60:
        flags.append({
            "type": "dominated_market",
            "severity": "medium",
            "message": f"Top 4 projects control {cr4:.0f}% of downloads"
        })

    # Declining interest
    if demand_detail.get("velocity_norm", 0) < 5:
        flags.append({
            "type": "declining_interest",
            "severity": "medium",
            "message": "Very low growth velocity — category may be stagnant"
        })

    # Niche audience
    if demand_detail.get("total_downloads", 0) < 100000:
        flags.append({
            "type": "niche_audience",
            "severity": "low",
            "message": f"Small total audience ({demand_detail['total_downloads']:,} downloads)"
        })

    # High concentration (HHI)
    hhi = supply_detail.get("hhi", 0)
    if hhi > 2500:
        flags.append({
            "type": "high_concentration",
            "severity": "high",
            "message": f"Market is concentrated (HHI: {hhi:.0f}) — few players dominate"
        })

    # Mature market
    avg_age = supply_detail.get("avg_age_days", 0)
    if avg_age > 1000:
        flags.append({
            "type": "mature_market",
            "severity": "medium",
            "message": f"Average project age is {avg_age:.0f} days — established market"
        })

    return flags


def compute_expected_growth_window(demand_detail, supply_detail):
    """Estimate the growth window for an opportunity.

    Returns a dict with:
    - window: "immediate" (< 30 days), "near_term" (30-90 days), "long_term" (90+ days)
    - time_to_enter: "build_now", "monitor", "avoid"
    - scenario_bands: conservative, base, aggressive growth estimates
    """
    velocity = demand_detail.get("velocity", 0)
    total_dl = demand_detail.get("total_downloads", 0)
    competitor_count = supply_detail.get("competitor_count", 0)
    cr4 = supply_detail.get("cr4", 0)

    # Time-to-enter recommendation
    if velocity > 500 and competitor_count < 100 and cr4 < 60:
        time_to_enter = "build_now"
    elif velocity > 100 and competitor_count < 300:
        time_to_enter = "monitor"
    else:
        time_to_enter = "avoid"

    # Growth window
    if velocity > 1000:
        window = "immediate"
        window_days = 30
    elif velocity > 200:
        window = "near_term"
        window_days = 60
    else:
        window = "long_term"
        window_days = 90

    # Scenario bands (daily downloads estimate for a new mod in this cluster)
    avg_per_project = total_dl / max(competitor_count, 1)
    daily_base = avg_per_project / 365  # rough daily average

    return {
        "window": window,
        "window_days": window_days,
        "time_to_enter": time_to_enter,
        "scenario_bands": {
            "conservative": int(daily_base * 0.5),
            "base": int(daily_base),
            "aggressive": int(daily_base * 2.0),
        },
    }


# =============================================================================
# VERSION+LOADER RECOMMENDATION ENGINE
# =============================================================================


def score_version_loader_pairs(cluster_projects, current_versions, baseline_version_map,
                                hours_between, loader_names):
    """Score every version+loader pair for a concept cluster.

    For each (game_version, loader) pair, compute:
    - Supply: number of projects in the cluster targeting this pair
    - Demand: total downloads growth for this pair within the cluster
    - Growth rate: how fast is this pair growing
    - Penalty: overcrowded pairs get penalized

    Returns a list of VL pair dicts sorted by opportunity score.
    """
    # Build a map of project_id -> {loaders, game_versions} from versions
    project_vl_map = defaultdict(lambda: {"loaders": set(), "game_versions": set()})
    for v in current_versions:
        pid = v.get("project_id", "")
        for ld in v.get("loaders", []) or []:
            project_vl_map[pid]["loaders"].add(ld)
        for gv in v.get("game_versions", []) or []:
            project_vl_map[pid]["game_versions"].add(gv)

    # Collect all game versions and loaders
    all_game_versions = set()
    all_loaders = set()
    for v in current_versions:
        for gv in v.get("game_versions", []) or []:
            all_game_versions.add(gv)
        for ld in v.get("loaders", []) or []:
            all_loaders.add(ld)

    # For each cluster project, track which VL pairs it supports
    cluster_project_ids = {p.get("project_id", "") for p in cluster_projects}

    # For each VL pair, compute supply and demand
    vl_pairs = {}
    for gv in all_game_versions:
        for ld in all_loaders:
            pair_key = (gv, ld)
            projects_in_pair = set()
            total_dl = 0
            total_delta = 0.0

            for pid in cluster_project_ids:
                vl_info = project_vl_map.get(pid, {"loaders": set(), "game_versions": set()})
                if ld in vl_info["loaders"] and gv in vl_info["game_versions"]:
                    projects_in_pair.add(pid)
                    # Find this project's downloads
                    for p in cluster_projects:
                        if p.get("project_id") == pid:
                            total_dl += p.get("downloads", 0)
                            break

            # Calculate delta for this VL pair
            for v in current_versions:
                pid = v.get("project_id", "")
                if pid not in projects_in_pair:
                    continue
                v_gv = set(v.get("game_versions", []) or [])
                v_ld = set(v.get("loaders", []) or [])
                if gv in v_gv and ld in v_ld:
                    vid = v.get("version_id", "")
                    current_dl = v.get("downloads", 0) or 0
                    baseline_dl = baseline_version_map.get(vid, 0)
                    if current_dl > baseline_dl:
                        total_delta += current_dl - baseline_dl

            if projects_in_pair:
                vl_pairs[pair_key] = {
                    "game_version": gv,
                    "loader": ld,
                    "project_count": len(projects_in_pair),
                    "total_downloads": total_dl,
                    "total_delta": round(total_delta, 1),
                }

    if not vl_pairs:
        return []

    # Calculate supply density (projects per pair)
    max_projects = max(v["project_count"] for v in vl_pairs.values()) if vl_pairs else 1
    max_delta = max(v["total_delta"] for v in vl_pairs.values()) if vl_pairs else 1

    # Score each VL pair
    scored_pairs = []
    for pair_key, data in vl_pairs.items():
        # Supply normalization (lower = better)
        supply_norm = (data["project_count"] / max_projects) * 100 if max_projects > 0 else 0

        # Demand normalization (higher = better)
        demand_norm = (data["total_delta"] / max_delta) * 100 if max_delta > 0 else 0

        # Opportunity score: demand - supply penalty
        # Boost under-served pairs (low supply, high demand)
        if data["project_count"] <= 5 and data["total_delta"] > 0:
            under_served_boost = 20.0
        elif data["project_count"] <= 10 and data["total_delta"] > 0:
            under_served_boost = 10.0
        else:
            under_served_boost = 0.0

        opportunity = max(0.0, demand_norm - supply_norm * 0.5 + under_served_boost)

        scored_pairs.append({
            **data,
            "supply_norm": round(supply_norm, 1),
            "demand_norm": round(demand_norm, 1),
            "opportunity_score": round(opportunity, 1),
            "under_served_boost": under_served_boost,
        })

    scored_pairs.sort(key=lambda x: x["opportunity_score"], reverse=True)
    return scored_pairs


# =============================================================================
# MAIN OPPORTUNITY ENGINE
# =============================================================================


def build_opportunity_analysis(current_snapshot, baseline_snapshot, hours_between,
                                loader_names, loader_set):
    """Build the full opportunity analysis.

    Args:
        current_snapshot: dict with projects, versions, total_downloads
        baseline_snapshot: dict with projects, versions (baseline)
        hours_between: hours between current and baseline
        loader_names: list of loader names
        loader_set: set of loader names for filtering

    Returns:
        dict with opportunities, concept_clusters, version_loader_recommendations
    """
    current_projects = current_snapshot.get("projects", [])
    current_versions = current_snapshot.get("versions", [])
    baseline_projects = baseline_snapshot.get("projects", [])
    baseline_versions = baseline_snapshot.get("versions", [])

    if not current_projects:
        return {"opportunities": [], "note": "no_project_data"}

    # Build baseline maps
    baseline_map = {p["project_id"]: p.get("downloads", 0) for p in baseline_projects}
    baseline_version_map = {v.get("version_id"): v.get("downloads", 0)
                            for v in baseline_versions if v.get("version_id")}

    # ── Step 1: Concept clustering ─────────────────────────────────
    # Assign each project to concept clusters
    cluster_projects = defaultdict(list)
    for p in current_projects:
        title = p.get("title", "")
        description = p.get("description", "")
        categories = p.get("categories", [])
        concepts = extract_concepts(title, description, categories)
        if concepts:
            for cluster_id, confidence in concepts:
                cluster_projects[cluster_id].append({
                    **p,
                    "_concept_confidence": confidence,
                })

    print(f"  Clustered {len(current_projects)} projects into {len(cluster_projects)} concept clusters")

    # Attach version data to cluster projects for VL scoring
    project_version_map = defaultdict(list)
    for v in current_versions:
        pid = v.get("project_id", "")
        if pid:
            project_version_map[pid].append(v)

    for cluster_id, projs in cluster_projects.items():
        for p in projs:
            pid = p.get("project_id", "")
            p["_versions"] = project_version_map.get(pid, [])

    # ── Step 2: Opportunity scoring per cluster ────────────────────
    opportunities = []
    for cluster_id, projs in cluster_projects.items():
        cluster_def = CONCEPT_CLUSTERS.get(cluster_id, {})
        if not cluster_def:
            continue

        demand_score, demand_detail = compute_demand_score(
            projs, baseline_map, hours_between, current_snapshot
        )
        supply_score, supply_detail = compute_supply_score(projs, baseline_map)
        opportunity_score = compute_opportunity_score(demand_score, supply_score)
        confidence_level, confidence_score = compute_confidence(
            demand_detail, supply_detail, projs
        )
        risk_flags = compute_risk_flags(demand_detail, supply_detail)
        growth_window = compute_expected_growth_window(demand_detail, supply_detail)

        # VL pair recommendations for this cluster
        vl_pairs = score_version_loader_pairs(
            projs, current_versions, baseline_version_map,
            hours_between, loader_names
        )

        # Best VL pair and next best
        best_pair = vl_pairs[0] if vl_pairs else None
        next_best_pair = vl_pairs[1] if len(vl_pairs) > 1 else None

        # Reason codes
        reasons = _build_reason_codes(demand_detail, supply_detail, best_pair, cluster_def)

        # Top example projects in this cluster
        top_examples = sorted(projs, key=lambda p: p.get("downloads", 0), reverse=True)[:5]
        top_example_names = [p.get("title", "") for p in top_examples]

        opportunities.append({
            "cluster_id": cluster_id,
            "cluster_label": cluster_def.get("label", cluster_id),
            "cluster_description": cluster_def.get("description", ""),
            "target_audience": cluster_def.get("target_audience", ""),
            "opportunity_score": opportunity_score,
            "demand_score": demand_score,
            "supply_score": supply_score,
            "confidence_level": confidence_level,
            "confidence_score": confidence_score,
            "risk_flags": risk_flags,
            "growth_window": growth_window,
            "recommended_version_loader": best_pair,
            "next_best_version_loader": next_best_pair,
            "top_vl_pairs": vl_pairs[:10],
            "reason_codes": reasons,
            "project_count": len(projs),
            "total_downloads_in_cluster": demand_detail.get("total_downloads", 0),
            "top_example_projects": top_example_names,
            "demand_detail": demand_detail,
            "supply_detail": supply_detail,
        })

    # Sort by opportunity score descending
    opportunities.sort(key=lambda x: x["opportunity_score"], reverse=True)

    # ── Step 3: Global VL pair recommendations ─────────────────────
    # Also score VL pairs across ALL projects (not just per cluster)
    all_vl_pairs = score_version_loader_pairs(
        [{"project_id": p.get("project_id", ""), "downloads": p.get("downloads", 0)}
         for p in current_projects],
        current_versions, baseline_version_map,
        hours_between, loader_names
    )

    # ── Step 4: Emerging concept detection ─────────────────────────
    # Detect concepts that are growing fast but have low supply
    emerging = []
    for opp in opportunities:
        if (opp["confidence_level"] in ("high", "medium") and
                opp["demand_score"] > 40 and
                opp["supply_score"] < 50 and
                opp["project_count"] < 200):
            emerging.append({
                "cluster_id": opp["cluster_id"],
                "cluster_label": opp["cluster_label"],
                "opportunity_score": opp["opportunity_score"],
                "growth_velocity": opp["demand_detail"].get("velocity", 0),
                "project_count": opp["project_count"],
                "top_examples": opp["top_example_projects"][:3],
            })
    emerging.sort(key=lambda x: x["opportunity_score"], reverse=True)

    return {
        "opportunities": opportunities,
        "top_10_opportunities": opportunities[:10],
        "global_vl_recommendations": all_vl_pairs[:20],
        "emerging_concepts": emerging[:10],
        "analysis_metadata": {
            "total_projects_analyzed": len(current_projects),
            "total_versions_analyzed": len(current_versions),
            "concept_clusters_found": len(cluster_projects),
            "hours_between_snapshots": hours_between,
            "engine_version": "1.0.0",
        },
    }


def _build_reason_codes(demand_detail, supply_detail, best_pair, cluster_def):
    """Build human-readable reason codes for why this opportunity is recommended."""
    reasons = []

    velocity = demand_detail.get("velocity", 0)
    if velocity > 1000:
        reasons.append({
            "code": "high_growth_velocity",
            "priority": "primary",
            "message": f"High growth velocity ({velocity:,.0f} downloads/hr) — strong demand signal"
        })
    elif velocity > 100:
        reasons.append({
            "code": "moderate_growth",
            "priority": "secondary",
            "message": f"Steady growth ({velocity:,.0f} downloads/hr) — consistent demand"
        })

    competitor_count = supply_detail.get("competitor_count", 0)
    if competitor_count < 50:
        reasons.append({
            "code": "low_competition",
            "priority": "primary",
            "message": f"Only {competitor_count} competitors — low supply, easy to stand out"
        })
    elif competitor_count < 150:
        reasons.append({
            "code": "moderate_competition",
            "priority": "secondary",
            "message": f"{competitor_count} competitors — room for differentiation"
        })

    cr4 = supply_detail.get("cr4", 0)
    if cr4 < 40:
        reasons.append({
            "code": "fragmented_market",
            "priority": "primary",
            "message": f"No dominant player (CR4: {cr4:.0f}%) — open field for new entrants"
        })

    follow_ratio = demand_detail.get("follow_ratio", 0)
    if follow_ratio > 0.01:
        reasons.append({
            "code": "high_engagement",
            "priority": "secondary",
            "message": f"High follow/download ratio ({follow_ratio*100:.2f}%) — engaged audience"
        })

    new_project_rate = demand_detail.get("new_project_rate", 0)
    if new_project_rate > 10:
        reasons.append({
            "code": "growing_ecosystem",
            "priority": "secondary",
            "message": f"New project rate {new_project_rate:.1f}% — expanding ecosystem"
        })

    if best_pair:
        gv = best_pair.get("game_version", "")
        ld = best_pair.get("loader", "")
        pair_count = best_pair.get("project_count", 0)
        if pair_count <= 10:
            reasons.append({
                "code": "underserved_vl_pair",
                "priority": "primary",
                "message": f"Only {pair_count} projects target {gv} {ld} — under-served pair"
            })

    return reasons