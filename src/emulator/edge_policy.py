"""
Temporarily neutralise Microsoft Edge's OS-backed sign-in during First Setup.

On Windows, Edge can pull the Microsoft identity from the logged-in Windows
account (Web Account Manager). Even on a brand-new profile, this can silently
authenticate the user and prevent them from choosing a different Rewards
account. During First Setup we apply a small set of HKCU Edge policies that
disable browser/profile sign-in and implicit OS sign-in, then restore every
previous value when setup exits.

Website sign-in remains available through the normal Microsoft login form.
No-op on non-Windows platforms.
"""

import platform

_POLICY_KEY = r"Software\Policies\Microsoft\Edge"
_VALUE_NAME = "BrowserSignin"
_USER_DATA_DIR_VALUE_NAME = "UserDataDir"

IDENTITY_ISOLATION_POLICIES = {
    # Disable signing the Edge browser profile itself into a Microsoft account.
    "BrowserSignin": 0,
    # Disable implicit browser sign-in from the Windows account / WAM identity.
    "ImplicitSignInEnabled": 0,
    # Do not turn a Microsoft website login into an Edge browser profile login.
    "WebToBrowserSignInEnabled": 0,
    # Do not silently bridge web sign-in back into browser profile sign-in.
    "SeamlessWebToBrowserSignInEnabled": 0,
}


def is_supported():
    """Return True on Windows where Edge policy edits are supported."""
    return platform.system() == "Windows"


def get_current_value():
    """Return the current BrowserSignin value (0/1/2) or None if unset / not supported."""
    return get_current_values().get(_VALUE_NAME)


def get_user_data_dir():
    """Return the current UserDataDir policy value, or None if unset / not supported."""
    if not is_supported():
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _POLICY_KEY, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            value, _ = winreg.QueryValueEx(key, _USER_DATA_DIR_VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def get_current_values():
    """
    Return current values for all identity-isolation policies.

    Missing values are represented as None so they can be deleted again during
    restore instead of being clobbered into a managed policy value.
    """
    if not is_supported():
        return {name: None for name in IDENTITY_ISOLATION_POLICIES}
    try:
        import winreg
    except ImportError:
        return {name: None for name in IDENTITY_ISOLATION_POLICIES}

    values = {}
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _POLICY_KEY, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            for name in IDENTITY_ISOLATION_POLICIES:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                    values[name] = int(value)
                except FileNotFoundError:
                    values[name] = None
    except FileNotFoundError:
        return {name: None for name in IDENTITY_ISOLATION_POLICIES}
    except OSError:
        return {name: None for name in IDENTITY_ISOLATION_POLICIES}
    return values


def set_browser_signin_disabled(disabled):
    """
    Set BrowserSignin=0 (disable) or delete the value (restore default behaviour).

    Args:
        disabled (bool): True to disable browser sign-in, False to restore default behaviour.

    Returns:
        bool: True if the operation was successful, False otherwise.
    """
    if disabled:
        return set_policy_values({_VALUE_NAME: 0})
    return restore_values({_VALUE_NAME: None})


def set_identity_isolation_enabled(enabled):
    """
    Apply or remove the First Setup identity-isolation policy set.

    Args:
        enabled (bool): True to write the policy set. False deletes these policy
            values, primarily for compatibility with the old single-value API.

    Returns:
        bool: True if the registry operation was successful, False otherwise.
    """
    if enabled:
        return set_policy_values(IDENTITY_ISOLATION_POLICIES)
    return restore_values({name: None for name in IDENTITY_ISOLATION_POLICIES})


def set_policy_values(values):
    """Write DWORD policy values under the current user's Edge policy key."""
    if not is_supported():
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _POLICY_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
        return True
    except OSError:
        return False


def set_user_data_dir(path):
    """
    Force Edge to use a specific user data directory while First Setup is open.

    Edge's managed UserDataDir policy overrides command-line --user-data-dir,
    so setting it temporarily prevents a policy or relaunch path from selecting
    the user's normal Edge profile instead of the account-scoped profile.
    """
    if not is_supported():
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _POLICY_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(
                key, _USER_DATA_DIR_VALUE_NAME, 0, winreg.REG_SZ, str(path)
            )
        return True
    except OSError:
        return False


def restore_value(previous_value):
    """
    Restore a previously-captured value (or delete the entry if it was unset).

    Args:
        previous_value (int or None): The value to restore, or None to delete the entry.
    """
    return restore_values({_VALUE_NAME: previous_value})


def restore_user_data_dir(previous_value):
    """Restore the previous UserDataDir policy value, or delete it if unset."""
    if not is_supported():
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _POLICY_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if previous_value is None:
                try:
                    winreg.DeleteValue(key, _USER_DATA_DIR_VALUE_NAME)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(
                    key, _USER_DATA_DIR_VALUE_NAME, 0, winreg.REG_SZ, str(previous_value)
                )
        return True
    except OSError:
        return False


def restore_values(previous_values):
    """
    Restore previously-captured Edge policy values.

    Args:
        previous_values (dict): Mapping of policy name to int value or None.
            None means the value was previously unset and should be deleted.
    """
    if not is_supported():
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _POLICY_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            for name, previous_value in previous_values.items():
                if previous_value is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(
                        key, name, 0, winreg.REG_DWORD, int(previous_value)
                    )
        return True
    except OSError:
        return False
