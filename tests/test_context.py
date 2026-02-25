from pocket.context import Context
from pocket.general_context import GeneralContext


def test_context_from_settings(base_settings):
    context = Context.from_settings(base_settings)
    assert context.general
    assert context.general.project_name == base_settings.project_name
    assert context.general.region == base_settings.region
    assert context.general.namespace == base_settings.namespace


def test_context_from_toml(use_toml, tmp_path):
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        """
[general]
region = "us-east-1"
project_name = "test-project"
stages = ["dev"]
namespace = "test"
"""
    )
    use_toml(str(toml_path))
    context = Context.from_toml(stage="dev")
    assert context.general
    assert context.general.project_name == "test-project"
    assert context.general.region == "us-east-1"
    assert context.general.namespace == "test"


def test_general_context_from_settings(base_settings):
    context = GeneralContext.from_general_settings(base_settings.general)
    assert context.project_name == base_settings.general.project_name
    assert context.region == base_settings.general.region
    assert context.namespace == base_settings.general.namespace


def test_general_context_from_toml(use_toml, tmp_path):
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        """
[general]
region = "us-east-1"
project_name = "test-project"
stages = ["dev"]
namespace = "test"
"""
    )
    use_toml(str(toml_path))
    context = GeneralContext.from_toml()
    assert context.project_name == "test-project"
    assert context.region == "us-east-1"
    assert context.namespace == "test"
