from pathlib import Path

import pytest
from test_resources_v34 import record

from cowmata_tailring.workspace import resource_import


@pytest.mark.parametrize('stage,title', [('early', '孕早期'), ('mid', '孕中期'), ('late', '孕晚期')])
def test_pregnancy_import_has_parent_scope(tmp_path, stage, title):
    source = record(tmp_path)
    plan = resource_import.plan_import(tmp_path/'resources', [{'path':str(source), 'kind':'imu'}],
                                      category='pregnancy_' + stage, cache=tmp_path/'cache')
    assert Path(plan['target']) == tmp_path/'resources/扬大_高邮牧场/怀孕'/title
    assert Path(plan['rows'][0]['target']).is_relative_to(Path(plan['target'])/'Motion')


def test_existing_farm_selection_does_not_repeat_farm(tmp_path):
    source = record(tmp_path)
    plan = resource_import.plan_import(tmp_path/'resources/扬大_高邮牧场', [{'path':str(source), 'kind':'imu'}],
                                      category='calving', cache=tmp_path/'cache')
    assert Path(plan['target']) == tmp_path/'resources/扬大_高邮牧场/产犊'


def test_stage_scope_switch_keeps_same_farm(tmp_path):
    source = record(tmp_path)
    plan = resource_import.plan_import(tmp_path/'resources/扬大_高邮牧场/怀孕/孕晚期', [{'path':str(source), 'kind':'imu'}],
                                      category='calving', cache=tmp_path/'cache')
    assert Path(plan['target']) == tmp_path/'resources/扬大_高邮牧场/产犊'


def test_organizer_requires_stage_and_submits_nested_category(monkeypatch):
    from PySide6.QtWidgets import QApplication, QWidget

    from cowmata_tailring.workspace.organization_ui import OrganizationWindow
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = OrganizationWindow(owner)
    calls = []
    monkeypatch.setattr(dialog, 'start_job', calls.append)
    dialog.category.setCurrentIndex(dialog.category.findData('pregnancy'))
    dialog.preview_import()
    assert not calls
    assert '孕期' in dialog.status.text()
    dialog.pregnancy_stage.setCurrentIndex(dialog.pregnancy_stage.findData('pregnancy_late'))
    dialog.preview_import()
    assert calls[-1]['category'] == 'pregnancy_late'
    dialog.category.setCurrentIndex(dialog.category.findData('calving'))
    dialog.preview_import()
    assert calls[-1]['category'] == 'calving'
    dialog.close()
    owner.close()
    app.processEvents()
