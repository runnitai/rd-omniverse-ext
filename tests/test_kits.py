"""Reading the kits listing, and the two tabs a kit is rendered through.

The server does the deciding: it resolves each kit's tools against the
catalogue, filters them to what this account can run, scopes the listing to this
plugin's surface, and drops a kit whose every tool is invisible. So the risk
here is not judgement, it is SHAPE, which is where every bug this plugin has had
actually lived. A kit's `kit_tags` and its tool's `kit_tags` are two different
things one field name apart, and `labels` is a third that looks like both.
"""

from __future__ import annotations

from rundiffusion_omniverse.api.kits import ALL_TAB, Kit, KitTool, parse_kits


def _tool(tool_id: str = "t1", **extra):
    return {
        "id": tool_id,
        "name": tool_id.title(),
        "description": "does a thing",
        "avatar_url": f"https://api.example/{tool_id}.png",
        **extra,
    }


def _entry(tool_id: str = "t1", kit_tags=(), labels=()):
    return {"tool": _tool(tool_id), "kit_tags": list(kit_tags), "labels": list(labels)}


def _kit(kit_id: str = "image", tools=None, **extra):
    return {
        "id": kit_id,
        "label": kit_id.title(),
        "icon": "mdi-image",
        "description": "Generate images from a prompt",
        "kit_tags": [],
        "default_tool_id": None,
        "tools": [_entry()] if tools is None else tools,
        **extra,
    }


class TestParsing:
    def test_a_kit_reads_as_the_server_sent_it(self):
        [kit] = parse_kits({"data": [_kit(kit_tags=["Featured", "Other"])]})

        assert kit.id == "image"
        assert kit.label == "Image"
        assert kit.description == "Generate images from a prompt"
        assert kit.kit_tags == ("Featured", "Other")

    def test_the_tool_inside_a_kit_is_a_tool_summary(self):
        """It arrives in the same shape `/tools` returns, which is what lets one
        renderer serve the browser and the dropdown."""
        [kit] = parse_kits({"data": [_kit()]})
        [entry] = kit.tools

        assert entry.tool.id == "t1"
        assert entry.tool.name == "T1"
        assert entry.tool.avatar_url == "https://api.example/t1.png"

    def test_stored_order_is_the_curation_and_is_kept(self):
        payload = {"data": [_kit(tools=[_entry("c"), _entry("a"), _entry("b")])]}

        [kit] = parse_kits(payload)

        assert [entry.tool.id for entry in kit.tools] == ["c", "a", "b"]

    def test_kit_order_is_the_servers(self):
        payload = {"data": [_kit("video"), _kit("image")]}

        assert [kit.id for kit in parse_kits(payload)] == ["video", "image"]

    def test_a_kit_with_nothing_in_it_is_skipped(self):
        """The server already drops these. Same rule surviving a partial
        response, not a second one: a card that opens onto an empty list is the
        failure both are avoiding."""
        payload = {"data": [_kit("image", tools=[]), _kit("video")]}

        assert [kit.id for kit in parse_kits(payload)] == ["video"]

    def test_an_entry_with_no_tool_is_dropped_rather_than_drawn(self):
        payload = {"data": [_kit(tools=[{"kit_tags": ["Featured"]}, _entry("t2")])]}

        [kit] = parse_kits(payload)

        assert [entry.tool.id for entry in kit.tools] == ["t2"]

    def test_a_label_keeps_its_text_and_drops_its_icon(self):
        """A label is `{id, icon, label}` and the icon is a Material Design
        Icons name. omni.ui cannot draw one, and drawing the literal string
        `mdi-flash` beside a tool would be worse than drawing nothing."""
        payload = {
            "data": [
                _kit(tools=[_entry(labels=[{"id": "l1", "icon": "mdi-flash", "label": "Fast"}])])
            ]
        }

        [kit] = parse_kits(payload)

        assert kit.tools[0].labels == ("Fast",)

    def test_a_label_with_no_text_is_not_an_empty_badge(self):
        payload = {"data": [_kit(tools=[_entry(labels=[{"id": "l1", "icon": "mdi-flash"}])])]}

        [kit] = parse_kits(payload)

        assert kit.tools[0].labels == ()

    def test_a_missing_label_falls_back_to_the_id_rather_than_blank(self):
        [kit] = parse_kits({"data": [{"id": "3d", "tools": [_entry()]}]})

        assert kit.label == "3d"

    def test_an_empty_body_is_no_kits_rather_than_an_error(self):
        """A picker that raises because an account has no curation would be a
        broken window; no kits is a legitimate answer."""
        assert parse_kits({"data": []}) == []
        assert parse_kits({}) == []
        assert parse_kits(None) == []


class TestTabs:
    def test_all_comes_first_and_is_this_panel_s_own(self):
        """`kit_tags` are the curator's tabs. Someone opening a kit wants to see
        what is in it before narrowing, and the server never sends an All."""
        kit = Kit(id="image", label="Image", kit_tags=("Featured", "Anime"))

        assert kit.tabs == (ALL_TAB, "Featured", "Anime")

    def test_all_shows_every_tool_whatever_it_is_tagged(self):
        kit = Kit(
            id="image",
            label="Image",
            kit_tags=("Featured",),
            tools=(
                KitTool(tool=_summary("a"), kit_tags=("Featured",)),
                KitTool(tool=_summary("b")),
            ),
        )

        assert [e.tool.id for e in kit.tools_for_tab(ALL_TAB)] == ["a", "b"]

    def test_a_tab_shows_only_what_the_curator_tagged(self):
        kit = Kit(
            id="image",
            label="Image",
            kit_tags=("Featured",),
            tools=(
                KitTool(tool=_summary("a"), kit_tags=("Featured",)),
                KitTool(tool=_summary("b"), kit_tags=("Anime",)),
            ),
        )

        assert [e.tool.id for e in kit.tools_for_tab("Featured")] == ["a"]

    def test_no_tab_selected_behaves_like_all(self):
        kit = Kit(id="image", label="Image", tools=(KitTool(tool=_summary("a")),))

        assert len(kit.tools_for_tab(None)) == 1

    def test_a_tab_keeps_the_curated_order(self):
        kit = Kit(
            id="image",
            label="Image",
            kit_tags=("Featured",),
            tools=(
                KitTool(tool=_summary("c"), kit_tags=("Featured",)),
                KitTool(tool=_summary("a"), kit_tags=("Featured",)),
            ),
        )

        assert [e.tool.id for e in kit.tools_for_tab("Featured")] == ["c", "a"]


def _summary(tool_id: str):
    from rundiffusion_omniverse.api.tools import ToolSummary

    return ToolSummary(id=tool_id, name=tool_id.title())


class TestTheRequest:
    """The URL, which carries the account and nothing else.

    There is deliberately no `?surface=` to get wrong: the server reads the
    client kind off the device record, so which curation this panel receives
    depends on `X-Device-Id` being on the request rather than on anything spelt
    here. That header is added by `session.auth_headers()` for every call, which
    makes it a reason not to remove it.
    """

    def test_it_asks_for_kits_with_the_team_the_run_would_be_billed_to(self):
        from rundiffusion_omniverse.api.constants import api_base_url
        from rundiffusion_omniverse.api.session import Account, RdSession
        from rundiffusion_omniverse.api.transport import with_query

        session = RdSession()
        session.accounts = [Account(id="t1", label="Acme", kind="TEAM")]
        session.selected_account_id = "t1"

        url = with_query(f"{api_base_url()}/kits", session.account_params())

        assert url == f"{api_base_url()}/kits?team_id=t1"

    def test_a_personal_account_omits_the_parameter_entirely(self):
        from rundiffusion_omniverse.api.constants import api_base_url
        from rundiffusion_omniverse.api.session import Account, RdSession
        from rundiffusion_omniverse.api.transport import with_query

        session = RdSession()
        session.accounts = [Account(id="p1", label="Personal", kind="PERSONAL")]

        url = with_query(f"{api_base_url()}/kits", session.account_params())

        assert url == f"{api_base_url()}/kits"
