"""Which approved skills a project, repository and role node may use. Pure policy, no I/O.

The catalog (``defaults/skill_catalog/catalog.json``) lists reviewed skills, the bundle(s)
each belongs to and the built-in roles it serves. A project profile picks bundles:

    {"bundles": ["core"], "repositories": {"<module>": {"bundles": ["web-react"]}},
     "roles": {"<role_id>": ["skill", ...]}, "exclude": ["skill", ...]}

Repository bundles add to the project bundles. ``roles`` adds skills to a role (custom roles
receive skills only this way); ``exclude`` removes skills everywhere.
"""

from agent_dev.domain.errors import DomainError, ErrorCode

SETTINGS_KEYS = {"bundles", "repositories", "roles", "exclude"}
# Per role node: at most this many skills and this many description characters.
NODE_SKILLS = 10
NODE_CHARS = 4000

# Repository marker -> suggested bundle. Detection only proposes; the person confirms.
MARKER_BUNDLES = {
    "react": "web-react",
    "expo": "mobile-expo",
    "react-native": "mobile-expo",
    "spring-boot": "spring",
    "android": "mobile-android",
    "flutter": "mobile-flutter",
    "swift": "mobile-ios",
    "postgres": "postgres",
    "docker": "container",
    "kubernetes": "container",
    "terraform": "terraform",
}


def _invalid(message):
    return DomainError(ErrorCode.INVALID_INPUT, message)


def _names(value, label):
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise _invalid(f"skills.{label} must be a list of names.")
    return list(dict.fromkeys(value))


def validate(catalog, settings, modules=()):
    """Normalized settings, or INVALID_INPUT for unknown bundles, skills or modules."""
    if settings in (None, {}):
        return {}
    if not isinstance(settings, dict) or set(settings) - SETTINGS_KEYS:
        raise _invalid("Unknown skills setting key.")
    bundles, skills = set(catalog["bundles"]), set(catalog["skills"])

    def known_bundles(values, label):
        values = _names(values, label)
        unknown = sorted(set(values) - bundles)
        if unknown:
            raise _invalid("Unknown skill bundle: " + ", ".join(unknown))
        return values

    def known_skills(values, label):
        values = _names(values, label)
        unknown = sorted(set(values) - skills)
        if unknown:
            raise _invalid("Unknown skill: " + ", ".join(unknown))
        return values

    result: dict = {"bundles": known_bundles(settings.get("bundles", []), "bundles")}
    repositories = settings.get("repositories", {})
    if not isinstance(repositories, dict):
        raise _invalid("skills.repositories must be an object.")
    if modules and set(repositories) - set(modules):
        raise _invalid("skills.repositories names an unknown repository.")
    result["repositories"] = {}
    for module, value in repositories.items():
        if not isinstance(value, dict) or set(value) - {"bundles"}:
            raise _invalid("skills.repositories entries only take bundles.")
        result["repositories"][module] = {
            "bundles": known_bundles(value.get("bundles", []), f"repositories.{module}.bundles")
        }
    roles = settings.get("roles", {})
    if not isinstance(roles, dict):
        raise _invalid("skills.roles must be an object.")
    result["roles"] = {
        role: known_skills(value, f"roles.{role}") for role, value in roles.items()
    }
    result["exclude"] = known_skills(settings.get("exclude", []), "exclude")
    return result


def sanitize(catalog, settings):
    """(settings, warnings) keeping only bundles and skills the catalog still knows.

    Saved profiles may outlive catalog entries; running work must not stop on that.
    """
    if not isinstance(settings, dict) or not settings:
        return {}, []
    bundles, skills, warnings = set(catalog["bundles"]), set(catalog["skills"]), []

    def keep(values, known, label):
        values = [v for v in values if isinstance(v, str)] if isinstance(values, list) else []
        dropped = [v for v in values if v not in known]
        if dropped:
            warnings.append(f"Unknown {label} ignored: " + ", ".join(dropped))
        return [v for v in dict.fromkeys(values) if v in known]

    repositories = settings.get("repositories", {})
    roles = settings.get("roles", {})
    result = {
        "bundles": keep(settings.get("bundles", []), bundles, "skill bundle"),
        "repositories": {
            module: {"bundles": keep(value.get("bundles", []), bundles, "skill bundle")}
            for module, value in (repositories.items() if isinstance(repositories, dict) else [])
            if isinstance(value, dict)
        },
        "roles": {
            role: keep(value, skills, "skill")
            for role, value in (roles.items() if isinstance(roles, dict) else [])
        },
        "exclude": keep(settings.get("exclude", []), skills, "skill"),
    }
    return result, warnings


def bundles_for(settings, module=None):
    """Project bundles plus the bundles of one repository, in catalog-independent order."""
    values = list(settings.get("bundles", []))
    if module:
        values += settings.get("repositories", {}).get(module, {}).get("bundles", [])
    return list(dict.fromkeys(values))


def repository_skills(catalog, settings, module):
    """Every skill a repository installs for interactive use (all roles)."""
    selected = set(bundles_for(settings, module))
    excluded = set(settings.get("exclude", []))
    names = [
        name
        for name, skill in catalog["skills"].items()
        if selected & set(skill["bundles"]) and name not in excluded
    ]
    for values in settings.get("roles", {}).values():
        names += [name for name in values if name not in excluded]
    return _ordered(catalog, names)


def _ordered(catalog, names):
    order = {bundle: index for index, bundle in enumerate(catalog["bundles"])}
    rank = {
        name: (min(order[b] for b in catalog["skills"][name]["bundles"]), index)
        for index, name in enumerate(catalog["skills"])
    }
    return sorted(dict.fromkeys(names), key=lambda name: rank[name])


def role_candidates(catalog, settings, role_id, module=None):
    """Ordered skills a role node may use before limits: mapped bundle skills, then extras."""
    selected = set(bundles_for(settings, module))
    excluded = set(settings.get("exclude", []))
    names = [
        name
        for name, skill in catalog["skills"].items()
        if role_id in skill.get("roles", []) and selected & set(skill["bundles"])
    ]
    names += settings.get("roles", {}).get(role_id, [])
    return [name for name in _ordered(catalog, names) if name not in excluded]


def apply_limits(role_id, names, descriptions=None):
    """(kept, warnings) under the per-node count and description-size limits."""
    kept, chars, warnings = [], 0, []
    for name in names:
        size = len((descriptions or {}).get(name, ""))
        if len(kept) >= NODE_SKILLS or chars + size > NODE_CHARS:
            warnings.append(f"{role_id}: skill {name} omitted by the per-node limit.")
            continue
        kept.append(name)
        chars += size
    return kept, warnings


def suggest(markers):
    """Bundles to propose for a repository from detected stack markers (core always)."""
    bundles = ["core"] + [MARKER_BUNDLES[m] for m in sorted(markers) if m in MARKER_BUNDLES]
    return list(dict.fromkeys(bundles))
