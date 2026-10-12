"""Platform additions keep authenticated IDs while lint uses effective order."""
from pathlib import Path
from unittest import mock
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_patches


def test_platform_number_gap_does_not_drop_integrity_checks():
    body = b'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'
    selected = [('patches/0001-one.patch', body),
                ('patches/platform/0225-first.patch', body),
                ('patches/platform/0227-second.patch', body)]
    seen = []
    def inspect(directory, verbose):
        names = (directory/'series').read_text().splitlines()
        assert names == ['0001-0001-one.patch', '0002-0225-first.patch', '0003-0227-second.patch']
        assert all((directory/name).read_bytes() == body for name in names)
        seen.append(names)
        return check_patches.Report()
    report = check_patches.Report()
    with mock.patch.object(check_patches.patch_selection, 'validate_overrides'), \
         mock.patch.object(check_patches.patch_selection, 'select', return_value=({'selection': {'version': 'test'}}, selected)), \
         mock.patch.object(check_patches, 'run_checks', side_effect=inspect):
        check_patches.check_selected_stacks(report, Path('.'))
    assert len(seen) == 3
    assert not report.failures


def test_changed_addition_still_rejected_by_selection():
    report = check_patches.Report()
    with mock.patch.object(check_patches.patch_selection, 'validate_overrides'), \
         mock.patch.object(check_patches.patch_selection, 'select', side_effect=ValueError('changed addition')):
        check_patches.check_selected_stacks(report, Path('.'))
    assert report.failures
