"""Buckets: the coarse profile that makes a room searchable without holding anyone's list.

Why this exists. A search is one exchange per agent, so a room of thousands and a network of millions
cannot be searched by asking everybody. The fix is a public, coarse profile: an agent declares which
groups its items fall into and how many items it holds in each, and the room indexes group to agent. A
search then runs real private exchanges with the few agents that share a group, not with everyone.

What a bucket is, and what it is not. In production the bucket space is the published taxonomy, 31
categories and 335 subcategories. This module carries a smaller fixture (25 groups, 500 item names) so the
mechanism can be measured here without depending on a file outside this repository; the mechanism does not
care how many groups there are. A bucket is never an item value: "logistics" is a bucket, "warehouse" is
an item. The bucket is visible to whoever holds the index, and that is the price of being searchable,
which the normative text has to state.

What is deliberately not here: any mapping from free text to a bucket. That is the intake's job (a person's
words to taxonomy slugs), it is product rather than protocol, and it does not change the shape of a search.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A fixture vocabulary: 25 groups, 20 item names each. Names are the kind of thing a buyer or seller would
# write; groups are the coarse bucket a profile would publish.
GROUPS: dict[str, tuple[str, ...]] = {
    "logistics": ("warehouse", "fulfilment", "last-mile", "freight", "customs", "packaging",
                  "cold-chain", "returns", "palletising", "cross-dock", "inventory", "route-planning",
                  "shipping", "port-handling", "rail", "air-cargo", "courier", "bonded-storage",
                  "tracking", "distribution"),
    "design": ("brand-design", "packaging-design", "industrial-design", "ux", "illustration",
               "motion", "typography", "art-direction", "design-system", "prototyping", "visual-identity",
               "wayfinding", "editorial", "iconography", "storyboarding", "wireframes", "renderings",
               "colour-grading", "type-design", "signage"),
    "software": ("backend", "frontend", "mobile", "data-pipeline", "machine-learning", "devops",
                 "qa", "security-review", "integration", "api-design", "refactoring", "migration",
                 "observability", "performance", "database", "search", "payments", "identity",
                 "infrastructure", "release-engineering"),
    "manufacturing": ("cnc", "injection-moulding", "sheet-metal", "pcb-assembly", "tooling",
                      "casting", "welding", "surface-finishing", "die-cutting", "inspection",
                      "assembly", "packaging-line", "3d-printing", "laser-cutting", "anodising",
                      "quality-control", "prototyping-run", "small-batch", "supply-of-parts", "tooling-design"),
    "energy": ("solar", "storage", "grid-connection", "ev-charging", "audit", "retrofit",
               "metering", "microgrid", "hydrogen", "wind", "heat-pump", "insulation",
               "carbon-accounting", "efficiency", "power-electronics", "battery", "substation",
               "load-balancing", "demand-response", "commissioning"),
    "finance": ("accounting", "treasury", "payroll", "tax", "audit-support", "fundraising",
                "valuation", "insurance", "credit", "payments-processing", "reconciliation",
                "compliance-reporting", "risk", "leasing", "invoicing", "escrow", "fx",
                "budgeting", "investor-reporting", "procurement-finance"),
    "legal": ("contracts", "incorporation", "ip", "trademark", "regulatory", "employment",
              "disputes", "data-protection", "licensing", "due-diligence", "corporate-secretarial",
              "immigration", "competition", "export-control", "consumer", "real-estate",
              "shipping-law", "arbitration", "policy", "training"),
    "health": ("clinical-trials", "diagnostics", "telemedicine", "regulatory-approval", "pharmacovigilance",
               "biostatistics", "patient-recruitment", "hospital-ops", "medical-devices", "quality-systems",
               "cold-chain-health", "lab-services", "genomics", "imaging", "rehabilitation",
               "pharmacy", "insurance-claims", "home-care", "screening", "health-data"),
    "agriculture": ("seeds", "irrigation", "harvesting", "cold-storage", "processing", "export-certification",
                    "soil", "fertiliser", "greenhouse", "livestock", "aquaculture", "machinery",
                    "organic-certification", "traceability", "packaging-fresh", "logistics-cold",
                    "quality-grading", "research-trials", "cooperatives", "finance-seasonal"),
    "education": ("curriculum", "teacher-training", "assessment", "e-learning", "language", "vocational",
                  "apprenticeship", "certification", "tutoring", "authoring", "simulation",
                  "laboratory", "careers", "admissions", "scholarships", "research", "publishing",
                  "accessibility", "mentoring", "campuses"),
    "retail": ("merchandising", "e-commerce", "pos", "fulfilment-retail", "customer-service", "loyalty",
               "pricing", "sourcing", "store-design", "visual-merchandising", "returns-retail",
               "marketplace-ops", "content-commerce", "advertising", "forecasting", "supplier-management",
               "packaging-retail", "category-management", "photography", "campaigns"),
    "media": ("video", "photography", "copywriting", "editing", "localisation", "seo", "social",
              "podcast", "animation", "post-production", "creative-direction", "media-buying",
              "public-relations", "events", "publishing-media", "rights", "distribution-media",
              "audience", "analytics-media", "scriptwriting"),
    "construction": ("structural", "architecture", "mep", "site-management", "quantity-surveying",
                     "permits", "fit-out", "facade", "concrete", "steel", "timber", "groundworks",
                     "roofing", "glazing", "interiors", "modular", "renovation", "surveying",
                     "safety", "commissioning-build"),
    "transport": ("trucking", "rail-freight", "air-freight", "sea-freight", "fleet", "drivers",
                  "scheduling", "telematics", "refuelling", "roadside", "warehousing",
                  "customs-transport", "dangerous-goods", "temperature-controlled", "intermodal",
                  "routing", "fleet-maintenance", "compliance-transport", "brokerage", "last-mile-transport"),
    "hospitality": ("hotels", "restaurants", "catering", "events-hospitality", "tourism", "travel",
                    "booking", "housekeeping", "kitchen-design", "suppliers-hospitality", "franchise",
                    "revenue-management", "guest-experience", "training-hospitality", "safety-hospitality",
                    "sustainability-hospitality", "beverages", "menus", "venues", "concierge"),
    "marketing": ("brand-strategy", "performance", "market-research", "copy", "design-marketing",
                  "crm", "automation", "attribution", "influencer", "partnerships", "field-marketing",
                  "events-marketing", "product-marketing", "pricing-marketing", "segmentation",
                  "content", "email", "community", "analytics-marketing", "brand-tracking"),
    "hr": ("recruitment", "executive-search", "compensation", "benefits", "learning", "engagement",
           "policy-hr", "hr-systems", "payroll-hr", "mobility", "succession", "dei", "workforce-planning",
           "contractors", "onboarding", "performance-management", "relations", "analytics-hr",
           "employer-brand", "assessment-hr"),
    "security": ("physical-security", "cyber", "penetration-testing", "monitoring", "incident-response",
                 "identity-security", "access-control", "surveillance", "guarding", "background-checks",
                 "fraud-prevention", "resilience", "audit-security", "training-security", "compliance-security",
                 "encryption", "network-security", "cloud-security", "forensics", "risk-security"),
    "environment": ("water", "waste", "recycling", "air-quality", "remediation", "monitoring-environment",
                    "permits-environment", "impact-assessment", "biodiversity", "carbon-offsets",
                    "circularity", "soil-remediation", "noise", "esg-reporting", "climate-risk",
                    "sustainability-strategy", "certification-environment", "research-environment",
                    "consulting-environment", "engineering-environment"),
    "research": ("surveys", "interviews", "ethnography", "data-collection", "statistics", "modelling",
                 "literature", "laboratory-research", "field-trials", "peer-review", "grant-writing",
                 "publishing-research", "replication", "benchmarks", "instrumentation", "ethics-review",
                 "data-management", "visualisation", "meta-analysis", "translation-research"),
    "public-sector": ("procurement-public", "policy-design", "service-delivery", "citizen-services",
                      "grants", "permits-public", "urban-planning", "housing", "social-services",
                      "statistics-public", "transparency", "audit-public", "training-public",
                      "emergency-planning", "infrastructure-public", "digital-government",
                      "engagement-public", "compliance-public", "funding", "partnerships-public"),
    "telecom": ("networks", "fibre", "wireless", "spectrum", "roaming", "billing", "oss-bss",
                "data-centres", "peering", "satellite", "iot-connectivity", "voice", "messaging",
                "coverage-planning", "towers", "field-telecom", "regulatory-telecom", "security-telecom",
                "support-telecom", "analytics-telecom"),
    "aerospace": ("airframes", "avionics", "engines", "mro", "certification-aero", "composites",
                  "ground-support", "simulators", "logistics-aero", "satellites", "drones", "payloads",
                  "safety-aero", "training-aero", "quality-aero", "supply-aero", "manufacturing-aero",
                  "testing-aero", "services-aero", "regulation-aero"),
    "chemistry": ("formulation", "polymers", "catalysts", "analysis-chemical", "synthesis", "coatings",
                  "adhesives", "specialty-chemicals", "petrochemicals", "lab-chemical", "safety-chemical",
                  "scale-up", "purification", "materials", "testing-chemical", "packaging-chemical",
                  "regulatory-chemical", "supply-chemical", "consulting-chemical", "engineering-chemical"),
    "sports": ("events-sports", "coaching", "facilities", "equipment", "sports-medicine", "media-sports",
               "tourism-sports", "retail-sports", "nutrition", "analytics-sports", "officiating",
               "grassroots", "sponsorship", "streaming-sports", "venues-sports", "travel-sports",
               "training-sports", "safety-sports", "marketing-sports", "operations-sports"),
}

# Every namespace in the vocabulary is searchable by bucket; goods namespaces are the ones a pair matches on.
QUANTITY_BUCKETS = len(GROUPS)
ITEM_NAMES = sum(len(items) for items in GROUPS.values())


def bucket_of(feature: str) -> str | None:
    """Which bucket a feature belongs to, or None when the fixture does not know it.

    A real deployment maps a feature to a taxonomy slug; here the fixture is the map. Unknown items are
    honest: an agent whose items fall outside the published buckets is not searchable by bucket, and a
    search that cannot find it says so rather than guessing.
    """
    from arli_engine.core.vocabulary import canonical_token

    token = canonical_token(feature)
    value = token.split(":", 1)[-1] if token.startswith(("city:", "industry:", "role:")) else token
    for group, items in GROUPS.items():
        if value in items:
            return group
    return None


def profile_of(features: list[str], bucket_of=None) -> dict[str, int]:
    """What an agent publishes: bucket to how many of its items land in that bucket. Never the items.

    The resolver is injectable because the bucket space is not always the fixture: the platform resolves an
    item to its taxonomy category, so an agent built from a written request lands in the same index as every
    other agent. A feature no resolver can place does not enter the profile, and a search therefore does not
    offer it, which the text has to say rather than pretend otherwise.
    """
    resolve = bucket_of or globals()["bucket_of"]
    profile: dict[str, int] = {}
    for feature in features:
        bucket = resolve(feature)
        if bucket:
            profile[bucket] = profile.get(bucket, 0) + 1
    return profile


@dataclass
class BucketIndex:
    """Bucket to agents, ranked by how much of the seeker's list falls in shared buckets.

    What this index holds: which agents declare items in which buckets, and counts. What it does not hold
    and cannot reveal: any item value. A search uses it to choose whom to run real exchanges with.
    """

    profiles: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, agent_id: str, features: list[str] | None = None,
            profile: dict[str, int] | None = None) -> None:
        self.profiles[agent_id] = dict(profile) if profile is not None else profile_of(features or [])

    def candidates(self, features: list[str], limit: int = 50) -> list[str]:
        """Agents worth an exchange, best first. An agent with no shared bucket is not worth one."""
        wanted = profile_of(features)
        if not wanted:
            return []
        scored = []
        for agent_id, profile in self.profiles.items():
            shared = [bucket for bucket in wanted if profile.get(bucket)]
            if not shared:
                continue
            # Overlap of buckets first (a real signal), then the agent's own depth, then the name so the
            # order does not depend on dictionary insertion.
            depth = sum(min(wanted[bucket], profile[bucket]) for bucket in shared)
            scored.append((-len(shared), -depth, agent_id))
        scored.sort()
        return [agent_id for _, _, agent_id in scored[:limit]]

    def size(self) -> int:
        return len(self.profiles)