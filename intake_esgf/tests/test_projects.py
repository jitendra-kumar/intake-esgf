import pytest

import intake_esgf
from intake_esgf.exceptions import ProjectNotSupported
from intake_esgf.projects import get_project, projects


def test_names_and_lookup():
    assert get_project("cmip7").name == "CMIP7"
    assert get_project("CMIP7") is get_project("cmip7")
    assert get_project("cordex-cmip6").name == "CORDEX-CMIP6"
    assert get_project("obs4mips").name == "obs4MIPs"
    for key, project in projects.items():
        assert project.name.lower() == key
        assert isinstance(project.facets, list)
    with pytest.raises(ProjectNotSupported):
        get_project("nope")


def test_supported_projects_unchanged():
    assert "CORDEX-CMIP6" in intake_esgf.supported_projects()

