def test_variable_info_not_available_is_assertion_error():
    from intake_esgf.exceptions import VariableInfoNotAvailable

    e = VariableInfoNotAvailable("CMIP7", "tas")
    assert isinstance(e, AssertionError)
    assert "CMIP7" in str(e) and "tas" in str(e)
