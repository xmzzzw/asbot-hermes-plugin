"""Private state and process-held file locks on POSIX and native Windows.

Windows ACLs are protected from inheritance and grant only the current SID.
PowerShell receives paths through argv, never interpolated script text. A failed
ACL check fails closed; chmod on Windows is deliberately not treated as privacy.
"""
import os
from pathlib import Path
import stat
import subprocess


def _windows_acl(path, create=False):
    script = r'''param([string]$Path,[string]$Create)
$ErrorActionPreference='Stop'
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$item=Get-Item -LiteralPath $Path -Force
if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {exit 2}
if ($Create -eq 'yes') {
 $acl=Get-Acl -LiteralPath $Path
 $acl.SetAccessRuleProtection($true,$false)
 foreach($rule in @($acl.Access)) {$acl.RemoveAccessRuleSpecific($rule)}
 $acl.SetOwner($sid)
 $inherit=if($item.PSIsContainer){[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'}else{[Security.AccessControl.InheritanceFlags]::None}
 $rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl',$inherit,'None','Allow')
 $acl.AddAccessRule($rule)
 Set-Acl -LiteralPath $Path -AclObject $acl
}
$acl=Get-Acl -LiteralPath $Path
if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value -or -not $acl.AreAccessRulesProtected) {exit 3}
$rules=$acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])
if ($rules.Count -eq 0) {exit 4}
foreach($rule in $rules) {if($rule.IdentityReference.Value -ne $sid.Value -or $rule.AccessControlType -ne 'Allow') {exit 5}}
'''
    # The script itself is fixed; all untrusted path characters remain an argument.
    import base64
    # -EncodedCommand cannot bind additional arguments consistently in PowerShell
    # 5.1. Supply parameters through a private environment block instead.
    script = script.replace('param([string]$Path,[string]$Create)', "$Path=$env:ASBOT_ACL_PATH; $Create=$env:ASBOT_ACL_CREATE")
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    env = dict(os.environ, ASBOT_ACL_PATH=str(path), ASBOT_ACL_CREATE='yes' if create else 'no')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
    if result.returncode:
        raise ValueError('PRIVATE_ACL_REQUIRED')


def secure_file(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ValueError('PRIVATE_FILE_REQUIRED')
    if os.name == 'nt':
        _windows_acl(path)
    elif info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('PRIVATE_FILE_REQUIRED')


def secure_dir(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('ABSOLUTE_DIRECTORY_REQUIRED')
    existed = path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError('PRIVATE_DIRECTORY_REQUIRED')
    if os.name == 'nt':
        _windows_acl(path, create=not existed)
    elif info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('PRIVATE_DIRECTORY_REQUIRED')


def secure_open(path, flags):
    path = Path(path)
    existed = path.exists() or path.is_symlink()
    if existed:
        secure_file(path)
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        if os.name == 'nt' and not existed:
            _windows_acl(path, create=True)
        secure_file(path)
        return fd
    except BaseException:
        os.close(fd)
        raise


def lock_exclusive(fd):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            raise BlockingIOError('LOCK_HELD') from None
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
