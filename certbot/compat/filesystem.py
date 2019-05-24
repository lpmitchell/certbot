"""Compat module to handle files security on Windows and Linux"""
from __future__ import absolute_import

import os  # pylint: disable=os-module-forbidden
import stat

try:
    import ntsecuritycon  # pylint: disable=import-error
    import win32security  # pylint: disable=import-error
except ImportError:
    POSIX_MODE = True
else:
    POSIX_MODE = False


def chmod(file_path, mode):
    # type: (str, int) -> None
    """
    Apply a POSIX mode on given file_path:
        * for Linux, the POSIX mode will be directly applied using chmod,
        * for Windows, the POSIX mode will be translated into a Windows DACL that make sense for
          Certbot context, and applied to the file using kernel calls.

    The definition of the Windows DACL that correspond to a POSIX mode, in the context of Certbot,
    is explained at https://github.com/certbot/certbot/issues/6356 and is implemented by the
    method _generate_windows_flags().

    :param str file_path: Path of the file
    :param int mode: POSIX mode to apply
    """
    if POSIX_MODE:
        os.chmod(file_path, mode)
    else:
        _apply_win_mode(file_path, mode)


def _apply_win_mode(file_path, mode):
    # Resolve symbolic links
    if os.path.islink(file_path):
        link_path = file_path
        file_path = os.readlink(file_path)
        if not os.path.isabs(file_path):
            file_path = os.path.join(os.path.dirname(link_path), file_path)
    # Get owner sid of the file
    security = win32security.GetFileSecurity(file_path, win32security.OWNER_SECURITY_INFORMATION)
    user = security.GetSecurityDescriptorOwner()

    # New DACL, that will overwrite existing one (including inherited permissions)
    dacl = _generate_dacl(user, mode)

    # Apply the new DACL
    security.SetSecurityDescriptorDacl(1, dacl, 0)
    win32security.SetFileSecurity(file_path, win32security.DACL_SECURITY_INFORMATION, security)


def _generate_dacl(user_sid, mode):
    analysis = _analyze_mode(mode)

    # Get standard accounts from "well-known" sid
    # See the list here:
    # https://support.microsoft.com/en-us/help/243330/well-known-security-identifiers-in-windows-operating-systems
    system = win32security.ConvertStringSidToSid('S-1-5-18')
    admins = win32security.ConvertStringSidToSid('S-1-5-32-544')
    everyone = win32security.ConvertStringSidToSid('S-1-1-0')

    # New dacl, without inherited permissions
    dacl = win32security.ACL()

    # If user is already system or admins, any ACE defined here would be superseeded by
    # the full control ACE that will be added after.
    if user_sid not in [system, admins]:
        # Handle user rights
        user_flags = _generate_windows_flags(analysis['user'])
        if user_flags:
            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, user_flags, user_sid)

    # Handle everybody rights
    everybody_flags = _generate_windows_flags(analysis['all'])
    if everybody_flags:
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, everybody_flags, everyone)

    # Handle administrator rights
    full_permissions = _generate_windows_flags({'read': True, 'write': True, 'execute': True})
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, full_permissions, system)
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, full_permissions, admins)

    return dacl


def _analyze_mode(mode):
    return {
        'user': {
            'read': mode & stat.S_IRUSR,
            'write': mode & stat.S_IWUSR,
            'execute': mode & stat.S_IXUSR,
        },
        'all': {
            'read': mode & stat.S_IROTH,
            'write': mode & stat.S_IWOTH,
            'execute': mode & stat.S_IXOTH,
        },
    }


def _generate_windows_flags(rights_desc):
    # Some notes about how each POSIX right is interpreted.
    #
    # For the rights read and execute, we have a pretty bijective relation between
    # POSIX flags and their generic counterparts on Windows, so we use them directly
    # (respectively ntsecuritycon.FILE_GENERIC_READ) and (respectively ntsecuritycon.FILE_GENERIC_EXECUTE).
    #
    # But ntsecuritycon.FILE_GENERIC_WRITE does not correspond to what one could expect from a write
    # access on Linux: for Windows, FILE_GENERIC_WRITE does not include delete, move or
    # rename. This is something that requires ntsecuritycon.FILE_ALL_ACCESS.
    # So to reproduce the write right as POSIX, we will apply ntsecuritycon.FILE_ALL_ACCESS
    # substracted of the rights corresponding to POSIX read and POSIX execute.
    #
    # Finally, having read + write + execute gives a ntsecuritycon.FILE_ALL_ACCESS,
    # so a "Full Control" on the file.
    #
    # A complete list of the rights defined on NTFS can be found here:
    # https://docs.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2003/cc783530(v=ws.10)#permissions-for-files-and-folders
    flag = 0
    if rights_desc['read']:
        flag = flag | ntsecuritycon.FILE_GENERIC_READ
    if rights_desc['write']:
        flag = flag | (ntsecuritycon.FILE_ALL_ACCESS
                       ^ ntsecuritycon.FILE_GENERIC_READ
                       ^ ntsecuritycon.FILE_GENERIC_EXECUTE
                       # Despite bit `512` being present in ntsecuritycon.FILE_ALL_ACCESS, it is
                       # not effectively applied to the file or the directory.
                       # As _generate_windows_flags is also used to compare two dacls, we remove
                       # it right now to have flags that contain only the bits effectively applied
                       # by Windows.
                       ^ 512)
    if rights_desc['execute']:
        flag = flag | ntsecuritycon.FILE_GENERIC_EXECUTE

    return flag


def _compare_dacls(dacl1, dacl2):
    aces1 = [dacl1.GetAce(index) for index in range(0, dacl1.GetAceCount())]
    aces2 = [dacl2.GetAce(index) for index in range(0, dacl2.GetAceCount())]

     # Convert PySIDs into hashable objects
    aces1_refined = []
    aces2_refined = []
    for ace in aces1:
        if len(ace) == 3:
            aces1_refined.append((ace[0], ace[1], str(ace[2])))
        else:
            aces1_refined.append((ace[0], ace[1], ace[2], ace[3], str(ace[4])))  # type: ignore
    for index, ace in enumerate(aces2):
        if len(ace) == 3:
            aces2_refined.append((ace[0], ace[1], str(ace[2])))
        else:
            aces2_refined.append((ace[0], ace[1], ace[2], ace[3], str(ace[4])))  # type: ignore

    return set(aces1_refined) == set(aces2_refined)
