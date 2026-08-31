"""Documentation drift guard.

The rule for this project is that the human guide and the LLM guide are
updated whenever the public API changes. A rule nobody can forget is better
than a rule in a checklist, so these tests fail the build when a public
method, exception, or model field goes undocumented.

If one of these fails, the fix is to document the thing - not to add it to an
exemption list.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import spotai
from spotai import SpotAI
from spotai.claims import Claim, ClaimResult, Clip
from spotai.sitemap import Camera, SiteMap

ROOT = Path(__file__).resolve().parent.parent
# The human guide IS the README, so it renders on the GitHub project page.
README = ROOT / "README.md"
HUMAN_GUIDE = README
LLM_GUIDE = ROOT / "llm" / "spotai-agent-guide.md"
GUIDE_POINTER = ROOT / "docs" / "GUIDE.md"

# Methods that are genuinely internal plumbing, documented as a module map
# rather than call-by-call. Keep this list short and justified.
LOW_LEVEL_METHODS = {
    "create_event",       # used via collect_damage_claim
    "create_device",      # used via collect_damage_claim
    "ensure_integration",
    "ensure_event_type",
}


def public_methods(cls) -> list[str]:
    return [
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]


@pytest.fixture(scope="module")
def llm_text() -> str:
    return LLM_GUIDE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def human_text() -> str:
    return HUMAN_GUIDE.read_text(encoding="utf-8")


class TestGuidesExist:
    def test_human_guide_is_the_readme(self):
        # Rendered on the GitHub project page, so it is the first thing seen.
        assert HUMAN_GUIDE.is_file()
        assert HUMAN_GUIDE.name == "README.md"

    def test_llm_guide_exists(self):
        assert LLM_GUIDE.is_file()

    def test_readme_links_the_llm_guide(self):
        assert "llm/spotai-agent-guide.md" in README.read_text(encoding="utf-8")

    def test_guide_pointer_does_not_duplicate_the_readme(self):
        # One copy to maintain; docs/GUIDE.md just points at it.
        assert GUIDE_POINTER.is_file()
        assert len(GUIDE_POINTER.read_text(encoding="utf-8")) < 1200


class TestApiSurfaceIsDocumented:
    def test_every_public_method_is_in_the_llm_guide(self, llm_text):
        missing = [m for m in public_methods(SpotAI) if m not in llm_text]
        assert not missing, (
            "Public SpotAI methods missing from llm/spotai-agent-guide.md: "
            + ", ".join(sorted(missing))
        )

    def test_user_facing_methods_are_in_the_human_guide(self, human_text):
        missing = [
            m
            for m in public_methods(SpotAI)
            if m not in LOW_LEVEL_METHODS and m not in human_text
        ]
        assert not missing, (
            "Methods missing from docs/GUIDE.md: " + ", ".join(sorted(missing))
        )

    def test_every_exported_name_is_in_the_llm_guide(self, llm_text):
        exported = [
            n for n in spotai.__all__ if n != "__version__"
        ]
        missing = [n for n in exported if n not in llm_text]
        assert not missing, (
            "Exported names missing from the LLM guide: " + ", ".join(missing)
        )


class TestModelFieldsAreDocumented:
    @pytest.mark.parametrize(
        "model", [Claim, ClaimResult, Clip, SiteMap, Camera]
    )
    def test_dataclass_fields_appear_in_the_llm_guide(self, model, llm_text):
        fields = [
            f for f in getattr(model, "__dataclass_fields__", {})
            if not f.startswith("_")
        ]
        missing = [f for f in fields if f not in llm_text]
        assert not missing, (
            model.__name__ + " fields missing from the LLM guide: "
            + ", ".join(missing)
        )


class TestCriticalWarningsSurvive:
    """These caused real breakage; they must never quietly leave the docs."""

    def test_url_expiry_is_warned_about_in_both_guides(self, llm_text, human_text):
        assert "hour" in llm_text.lower()
        assert "hour" in human_text.lower()

    def test_seven_day_retention_is_stated(self, llm_text, human_text):
        assert "7 day" in llm_text.lower() or "7-day" in llm_text.lower()
        assert "7 day" in human_text.lower() or "7-day" in human_text.lower()

    def test_missing_role_diagnosis_is_documented(self, llm_text, human_text):
        assert "Role" in llm_text and "Role" in human_text

    def test_partial_status_is_explained(self, llm_text, human_text):
        assert "partial" in llm_text and "partial" in human_text


class TestVersionsAgree:
    def test_llm_guide_names_the_current_version(self, llm_text):
        assert spotai.__version__ in llm_text, (
            "The LLM guide references a different version than "
            "spotai.__version__ (" + spotai.__version__ + ")"
        )


class TestSiteMapGuide:
    """The camera-directory procedure is the hardest part to get right, so it
    gets its own guide and its own guard."""

    SITE_MAP_GUIDE = ROOT / "llm" / "build-site-map-guide.md"

    def test_guide_exists(self):
        assert self.SITE_MAP_GUIDE.is_file()

    def test_readme_links_it(self):
        assert "llm/build-site-map-guide.md" in README.read_text(encoding="utf-8")

    def test_main_agent_guide_links_it(self):
        assert "build-site-map-guide.md" in LLM_GUIDE.read_text(encoding="utf-8")

    def test_covers_the_scale_that_breaks_naive_setups(self):
        text = self.SITE_MAP_GUIDE.read_text(encoding="utf-8")
        assert "23" in text          # arches + tunnel at full size
        assert "16" in text          # the share-link cap
        assert "4" in text           # cameras per device

    def test_warns_that_order_cannot_be_inferred(self):
        text = self.SITE_MAP_GUIDE.read_text(encoding="utf-8").lower()
        assert "no position" in text or "cannot be derived" in text

    def test_explains_the_shared_arch_offset_rule(self):
        text = self.SITE_MAP_GUIDE.read_text(encoding="utf-8").lower()
        assert "arch" in text and "same" in text
