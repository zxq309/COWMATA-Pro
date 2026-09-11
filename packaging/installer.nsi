; Compile with /INPUTCHARSET UTF8. Unicode output alone does not set input encoding.
Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"
!include "nsDialogs.nsh"

!ifndef COMPRESSION
  !define COMPRESSION lzma
!endif
!if "${COMPRESSION}" == "lzma"
  ; Independent blocks: small download without whole-package temp expansion.
  SetCompressor lzma
  SetCompressorDictSize 16
!else
  ; Independent blocks avoid whole-payload temporary decompression.
  SetCompressor zlib
!endif
SetDatablockOptimize on
CRCCheck force
Name "COWMATA Annotator ${VERSION}"
BrandingText "COWMATA · www.cowmata.com"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\COWMATA Annotator"
RequestExecutionLevel user
SetFont "Microsoft YaHei UI" 9
ShowInstDetails nevershow
ShowUninstDetails nevershow
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "COWMATA Annotator"
VIAddVersionKey "FileDescription" "COWMATA Offline Setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "COWMATA contributors"

!define MUI_ICON "${PACKAGE}\assets\app-icon\cowmata.ico"
!define MUI_UNICON "${PACKAGE}\assets\app-icon\cowmata.ico"
!define MUI_BGCOLOR "F3F7F0"
!define MUI_TEXTCOLOR "20332A"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "${PACKAGE}\assets\app-icon\installer-header.bmp"
!define MUI_INSTFILESPAGE_COLORS "20332A F3F7F0"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${PACKAGE}\assets\app-icon\installer.bmp"
!define MUI_WELCOMEPAGE_TITLE "$(WelcomeTitle)"
!define MUI_WELCOMEPAGE_TEXT "$(WelcomeBody)"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${PACKAGE}\LICENSE"
Page custom LocationCreate LocationLeave
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TITLE "$(FinishTitle)"
!define MUI_FINISHPAGE_TEXT "$(FinishBody)"
!define MUI_FINISHPAGE_RUN "$INSTDIR\COWMATA.exe"
!define MUI_FINISHPAGE_RUN_TEXT "$(LaunchText)"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

LangString WelcomeTitle ${LANG_SIMPCHINESE} "安装 COWMATA 标注工具"
LangString WelcomeTitle ${LANG_ENGLISH} "Install COWMATA Annotator"
LangString WelcomeBody ${LANG_SIMPCHINESE} "录像与九轴数据同步标注工作台。$\r$\n$\r$\n完整离线安装，已包含运行库和模型，不需要配置 Python。$\r$\n$\r$\n接下来选择存放位置，安装器会自动创建软件文件夹。"
LangString WelcomeBody ${LANG_ENGLISH} "Video and nine-axis annotation workstation.$\r$\n$\r$\nAll runtimes and models are included. No Python setup or downloads.$\r$\n$\r$\nSelect a location; Setup creates the application folder."
LangString LocationTitle ${LANG_SIMPCHINESE} "选择安装位置"
LangString LocationTitle ${LANG_ENGLISH} "Choose installation location"
LangString LocationBody ${LANG_SIMPCHINESE} "选择存放位置，自动创建软件文件夹；名称可以修改。"
LangString LocationBody ${LANG_ENGLISH} "Choose a location; Setup creates the named application folder."
LangString ParentLabel ${LANG_SIMPCHINESE} "存放位置"
LangString ParentLabel ${LANG_ENGLISH} "Parent location"
LangString FolderLabel ${LANG_SIMPCHINESE} "软件文件夹名"
LangString FolderLabel ${LANG_ENGLISH} "Application folder name"
LangString BrowseText ${LANG_SIMPCHINESE} "浏览…"
LangString BrowseText ${LANG_ENGLISH} "Browse..."
LangString FinalLabel ${LANG_SIMPCHINESE} "实际安装到："
LangString FinalLabel ${LANG_ENGLISH} "Install into:"
LangString DesktopText ${LANG_SIMPCHINESE} "创建桌面快捷方式"
LangString DesktopText ${LANG_ENGLISH} "Create a desktop shortcut"
LangString SpaceText ${LANG_SIMPCHINESE} "需要约 2 GB 空间。录像、九轴和标签请保存在软件目录之外。"
LangString SpaceText ${LANG_ENGLISH} "About 2 GB required. Keep recordings and labels outside this folder."
LangString InvalidLocation ${LANG_SIMPCHINESE} "请选择本地磁盘的有效绝对路径，文件夹名不能包含特殊字符、末尾空格或点，完整路径最长 100 字符。"
LangString InvalidLocation ${LANG_ENGLISH} "Choose a valid absolute local path (maximum 100 characters). No special characters, trailing dots or spaces in folder names."
LangString OccupiedLocation ${LANG_SIMPCHINESE} "这个软件文件夹已包含文件。请换一个软件文件夹名，以免覆盖已有软件或数据。"
LangString OccupiedLocation ${LANG_ENGLISH} "The application folder is not empty. Choose another folder name to preserve existing files."
LangString NoSpace ${LANG_SIMPCHINESE} "磁盘空间不足或目录不可写。请更换存放位置。"
LangString NoSpace ${LANG_ENGLISH} "Insufficient disk space or the location is not writable. Choose another location."
LangString FinishTitle ${LANG_SIMPCHINESE} "COWMATA Annotator 安装完成"
LangString FinishTitle ${LANG_ENGLISH} "COWMATA Annotator is installed"
LangString FinishBody ${LANG_SIMPCHINESE} "运行库与模型已就绪，无需额外配置。$\r$\n$\r$\n可从桌面快捷方式（如已勾选）或开始菜单打开。$\r$\n$\r$\n点击“完成”退出安装向导。"
LangString FinishBody ${LANG_ENGLISH} "Runtimes and models are ready. No additional setup.$\r$\n$\r$\nOpen from Start or the desktop shortcut, if selected.$\r$\n$\r$\nClick Finish to close Setup."
LangString LaunchText ${LANG_SIMPCHINESE} "启动 COWMATA Annotator"
LangString LaunchText ${LANG_ENGLISH} "Launch COWMATA Annotator"
LangString InstallFailure ${LANG_SIMPCHINESE} "安装未完成。请检查可用空间和目录权限；原始数据未被修改。"
LangString InstallFailure ${LANG_ENGLISH} "Installation failed. Check disk space and permissions. Original data was not changed."
LangString UninstallBusy ${LANG_SIMPCHINESE} "文件正在使用或无法删除。请保存标注、完全退出软件后重试卸载；不要手工强删。"
LangString UninstallBusy ${LANG_ENGLISH} "Files are in use or could not be removed. Save your work, close the application and retry uninstall."
LangString UninstallUnsafe ${LANG_SIMPCHINESE} "安装目录中有链接、重定向目录或异常文件。为保护其他位置的数据，本次卸载已停止。"
LangString UninstallUnsafe ${LANG_ENGLISH} "A linked, redirected or invalid application directory was found. Uninstall stopped to protect other locations."
LangString UninstallRetained ${LANG_SIMPCHINESE} "软件文件已移除。安装目录中还有不属于软件的内容，已保留：$\r$\n$INSTDIR$\r$\n$\r$\n请核对是否为您存放的录像、九轴或标签，再自行决定如何处理。"
LangString UninstallRetained ${LANG_ENGLISH} "Software files were removed. Unrecognized contents were preserved in:$\r$\n$INSTDIR$\r$\n$\r$\nReview recordings, sensor data and labels before removing these yourself."

Var ParentPath
Var FolderName
Var ParentEdit
Var FolderEdit
Var FinalPathLabel
Var DesktopCheck
Var DesktopEnabled
Var PathError
Var StageOnly
Var RegisterOnly

Function .onInit
  SetShellVarContext current
  StrCpy $DesktopEnabled ${BST_CHECKED}
  ${GetParameters} $R0
  ${GetOptions} $R0 "/STAGE=" $StageOnly
  ${GetOptions} $R0 "/REGISTERONLY=" $RegisterOnly
  ${GetOptions} $R0 "/DESKTOP=" $R1
  ${If} $R1 == "0"
    StrCpy $DesktopEnabled ${BST_UNCHECKED}
  ${EndIf}
  ClearErrors
  ${GetParent} "$INSTDIR" $ParentPath
  ${GetFileName} "$INSTDIR" $FolderName
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "COWMATA requires Windows 10/11 x64."
    Abort
  ${EndIf}
FunctionEnd

Function LocationCreate
  !insertmacro MUI_HEADER_TEXT "$(LocationTitle)" "$(LocationBody)"
  nsDialogs::Create 1018
  Pop $0
  SetCtlColors $0 20332A F3F7F0
  ${NSD_CreateLabel} 0 0 100% 12u "$(ParentLabel)"
  Pop $0
  ${NSD_CreateText} 0 16u 80% 14u "$ParentPath"
  Pop $ParentEdit
  ${NSD_OnChange} $ParentEdit LocationChanged
  ${NSD_CreateButton} 82% 15u 18% 16u "$(BrowseText)"
  Pop $0
  ${NSD_OnClick} $0 BrowseParent
  ${NSD_CreateLabel} 0 33u 100% 12u "$(FolderLabel)"
  Pop $0
  ${NSD_CreateText} 0 47u 100% 14u "$FolderName"
  Pop $FolderEdit
  ${NSD_OnChange} $FolderEdit LocationChanged
  ${NSD_CreateLabel} 0 68u 100% 12u "$(FinalLabel)"
  Pop $0
  ${NSD_CreateLabel} 0 82u 100% 20u "$INSTDIR"
  Pop $FinalPathLabel
  ${NSD_CreateCheckbox} 0 107u 100% 14u "$(DesktopText)"
  Pop $DesktopCheck
  ${NSD_SetState} $DesktopCheck $DesktopEnabled
  ${NSD_CreateLabel} 0 124u 100% 16u "$(SpaceText)"
  Pop $0
  nsDialogs::Show
FunctionEnd

Function BrowseParent
  Pop $0
  ${NSD_GetText} $ParentEdit $ParentPath
  nsDialogs::SelectFolderDialog "$(ParentLabel)" "$ParentPath"
  Pop $0
  ${If} $0 != error
    ${NSD_SetText} $ParentEdit $0
  ${EndIf}
FunctionEnd

Function LocationChanged
  Pop $0
  Call UpdateLocation
FunctionEnd

Function UpdateLocation
  ${NSD_GetText} $ParentEdit $ParentPath
  ${NSD_GetText} $FolderEdit $FolderName
  StrCpy $0 $ParentPath 1 -1
  ${If} $0 == "\"
    StrCpy $INSTDIR "$ParentPath$FolderName"
  ${Else}
    StrCpy $INSTDIR "$ParentPath\$FolderName"
  ${EndIf}
  ${NSD_SetText} $FinalPathLabel "$INSTDIR"
FunctionEnd

; Validate without changing the target. Silent installs use this too.
Function ValidateLocation
  StrCpy $PathError "$(InvalidLocation)"
  StrLen $0 $INSTDIR
  ${If} $0 < 4
  ${OrIf} $0 > 100
    Return
  ${EndIf}
  StrCpy $0 $INSTDIR 2 1
  ${If} $0 != ":\"
    Return
  ${EndIf}
  ; Win32 canonicalization also accepts a folder that has not been created yet.
  System::Call 'kernel32::GetFullPathNameW(w "$INSTDIR", i ${NSIS_MAX_STRLEN}, w .r0, p 0) i.r1'
  ${If} $0 != $INSTDIR
    Return
  ${EndIf}
  StrCpy $0 $INSTDIR 1 -1
  ${If} $0 == "."
  ${OrIf} $0 == " "
  ${OrIf} $0 == "\"
    Return
  ${EndIf}
  System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR") i.r0'
  ${If} $0 <> -1
    IntOp $1 $0 & 0x410
    ${If} $1 <> 16
      Return
    ${EndIf}
  ${EndIf}
  ClearErrors
  ; Registration-only never extracts over existing files.
  ${If} $RegisterOnly == "1"
    FileOpen $0 "$INSTDIR\COWMATA.install-id" r
    IfErrors register_invalid
    FileRead $0 $1
    FileClose $0
    ${If} $1 == "COWMATA-${VERSION}"
      StrCpy $PathError ""
    ${EndIf}
    register_invalid:
    Return
  ${EndIf}
  FindFirst $0 $1 "$INSTDIR\*"
  ${DoUntil} ${Errors}
    ${If} $1 != "."
    ${AndIf} $1 != ".."
      FindClose $0
      StrCpy $PathError "$(OccupiedLocation)"
      Return
    ${EndIf}
    FindNext $0 $1
  ${Loop}
  FindClose $0
  ClearErrors
  StrCpy $PathError ""
FunctionEnd

Function LocationLeave
  Call UpdateLocation
  ${NSD_GetState} $DesktopCheck $DesktopEnabled
  ${NSD_GetText} $FolderEdit $FolderName
  StrLen $1 $FolderName
  ${If} $1 == 0
    MessageBox MB_ICONEXCLAMATION "$(InvalidLocation)"
    Abort
  ${EndIf}
  StrCpy $0 0
  ${DoWhile} $0 < $1
    StrCpy $2 $FolderName 1 $0
    ${If} $2 == "\"
    ${OrIf} $2 == "/"
    ${OrIf} $2 == ":"
    ${OrIf} $2 == "*"
    ${OrIf} $2 == "?"
    ${OrIf} $2 == '$\"'
    ${OrIf} $2 == "<"
    ${OrIf} $2 == ">"
    ${OrIf} $2 == "|"
      MessageBox MB_ICONEXCLAMATION "$(InvalidLocation)"
      Abort
    ${EndIf}
    IntOp $0 $0 + 1
  ${Loop}
  Call ValidateLocation
  ${If} $PathError != ""
    MessageBox MB_ICONEXCLAMATION "$PathError"
    Abort
  ${EndIf}
  StrCpy $0 $INSTDIR 3
  System::Call 'kernel32::GetDiskFreeSpaceExW(w r0, *l .r1, p 0, p 0) i.r2'
  ${If} $2 == 0
    MessageBox MB_ICONEXCLAMATION "$(NoSpace)"
    Abort
  ${EndIf}
  System::Int64Op $1 / 1048576
  Pop $1
  ${If} $1 < 2200
    MessageBox MB_ICONEXCLAMATION "$(NoSpace)"
    Abort
  ${EndIf}
FunctionEnd

Section "COWMATA" Main
  Call ValidateLocation
  ${If} $PathError != ""
    IfSilent +2
      MessageBox MB_ICONSTOP "$PathError"
    SetErrorLevel 2
    Abort
  ${EndIf}
  ${If} $RegisterOnly == "1"
    Goto register_installation
  ${EndIf}
  ClearErrors
  SetDetailsPrint none
  SetOutPath "$INSTDIR"
  !include "${INSTALL_LIST}"
  ${If} ${Errors}
    IfSilent +2
      MessageBox MB_ICONSTOP "$(InstallFailure)"
    SetErrorLevel 3
    Abort
  ${EndIf}
  FileOpen $0 "$INSTDIR\COWMATA.install-id" w
  FileWrite $0 "COWMATA-${VERSION}"
  FileClose $0
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  ${If} $StageOnly == "1"
    Goto install_done
  ${EndIf}
  register_installation:
  SetOutPath "$INSTDIR"
  CreateDirectory "$SMPROGRAMS\COWMATA Annotator ${VERSION}"
  CreateShortcut "$SMPROGRAMS\COWMATA Annotator ${VERSION}\COWMATA Annotator.lnk" "$INSTDIR\COWMATA.exe" "" "$INSTDIR\COWMATA.exe" 0
  CreateShortcut "$SMPROGRAMS\COWMATA Annotator ${VERSION}\Uninstall.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\Uninstall.exe" 0
  nsExec::ExecToStack '"$INSTDIR\COWMATA.exe" --register-shortcut "$SMPROGRAMS\COWMATA Annotator ${VERSION}\COWMATA Annotator.lnk"'
  Pop $0
  Pop $1
  ${If} $0 != 0
    SetErrors
  ${EndIf}
  ${If} $DesktopEnabled == ${BST_CHECKED}
    CreateShortcut "$DESKTOP\COWMATA Annotator ${VERSION}.lnk" "$INSTDIR\COWMATA.exe" "" "$INSTDIR\COWMATA.exe" 0
    nsExec::ExecToStack '"$INSTDIR\COWMATA.exe" --register-shortcut "$DESKTOP\COWMATA Annotator ${VERSION}.lnk"'
    Pop $0
    Pop $1
    ${If} $0 != 0
      SetErrors
    ${EndIf}
  ${EndIf}
  ; Refresh only this application's changed EXE and shortcut; never reset the
  ; user's global icon cache or restart Explorer during an upgrade.
  System::Call 'shell32::SHChangeNotify(i 0x2000, i 0x1005, w "$INSTDIR\COWMATA.exe", p 0)'
  System::Call 'shell32::SHChangeNotify(i 0x2000, i 0x1005, w "$SMPROGRAMS\COWMATA Annotator ${VERSION}\COWMATA Annotator.lnk", p 0)'
  ${If} $DesktopEnabled == ${BST_CHECKED}
    System::Call 'shell32::SHChangeNotify(i 0x2000, i 0x1005, w "$DESKTOP\COWMATA Annotator ${VERSION}.lnk", p 0)'
  ${EndIf}
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "DisplayName" "COWMATA Annotator ${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "DisplayIcon" "$INSTDIR\COWMATA.exe,0"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "NoRepair" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "DesktopShortcut" $DesktopEnabled
  ${If} ${Errors}
    IfSilent +2
      MessageBox MB_ICONSTOP "$(InstallFailure)"
    SetErrorLevel 3
    Abort
  ${EndIf}
  install_done:
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  FileOpen $0 "$INSTDIR\COWMATA.install-id" r
  IfErrors unsafe
  FileRead $0 $1
  FileClose $0
  StrCmp $1 "COWMATA-${VERSION}" 0 unsafe
  ; Reject redirected ancestors and a running executable before removing files.
  Push "$INSTDIR"
  Call un.CheckDirectory
  IfFileExists "$INSTDIR\COWMATA.exe" 0 process_check_done
    nsExec::ExecToStack /TIMEOUT=15000 '"$INSTDIR\COWMATA.exe" --check-running'
    Pop $0
    Pop $1
    ${If} $0 != 0
      IfSilent +2
        MessageBox MB_ICONSTOP "$(UninstallBusy)"
      SetErrorLevel 5
      Abort
    ${EndIf}
  process_check_done:
  Push "$INSTDIR\COWMATA.exe"
  Call un.CheckNotInUse
  Push "$INSTDIR\runtime\pythonw.exe"
  Call un.CheckNotInUse
  Push "$INSTDIR\runtime\python.exe"
  Call un.CheckNotInUse
  Push "$INSTDIR\model_runtime_20260906\python.exe"
  Call un.CheckNotInUse
  ; Shipped files + narrowly matched generated bytecode/logs; no recursive delete.
  !include "${UNINSTALL_LIST}"
  ClearErrors
  Delete "$INSTDIR\Uninstall.exe"
  Call un.CheckDeleteErrors
  Delete "$INSTDIR\COWMATA.install-id"
  SetOutPath "$TEMP"
  RMDir "$INSTDIR"
  ; Older copies cannot remove registration/shortcuts belonging to a newer copy.
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "InstallLocation"
  ${If} $0 == $INSTDIR
    Delete "$DESKTOP\COWMATA Annotator ${VERSION}.lnk"
    Delete "$SMPROGRAMS\COWMATA Annotator ${VERSION}\COWMATA Annotator.lnk"
    Delete "$SMPROGRAMS\COWMATA Annotator ${VERSION}\Uninstall.lnk"
    RMDir "$SMPROGRAMS\COWMATA Annotator ${VERSION}"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}"
  ${EndIf}
  Goto done
  unsafe:
    IfSilent +2
      MessageBox MB_ICONSTOP "Installation identity does not match. No files were removed."
    SetErrorLevel 2
    Abort
  done:
    IfFileExists "$INSTDIR\*.*" 0 +3
      IfSilent +2
        MessageBox MB_ICONINFORMATION "$(UninstallRetained)"
SectionEnd

; Every owned directory and its ancestors are checked BEFORE generated deletion.
Function un.CheckDirectory
  Pop $R0
  StrLen $R2 $R0
  ${DoWhile} $R2 > 3
    System::Call 'kernel32::GetFileAttributesW(w R0) i.R1'
    ${If} $R1 <> -1
      IntOp $R1 $R1 & 0x410
      ${If} $R1 <> 16
        IfSilent +2
          MessageBox MB_ICONSTOP "$(UninstallUnsafe)"
        SetErrorLevel 4
        Abort
      ${EndIf}
    ${EndIf}
    ${GetParent} "$R0" $R0
    StrLen $R2 $R0
  ${Loop}
FunctionEnd

Function un.CheckNotInUse
  Pop $R0
  ; Request delete access, never truncate or write the candidate.
  System::Call 'kernel32::CreateFileW(w R0, i 0x10000, i 7, p 0, i 3, i 0x80, p 0) p.R1 ?e'
  Pop $R2
  ${If} $R1 = -1
    ${If} $R2 != 2
    ${AndIf} $R2 != 3
      IfSilent +2
        MessageBox MB_ICONSTOP "$(UninstallBusy)"
      SetErrorLevel 5
      Abort
    ${EndIf}
  ${Else}
    System::Call 'kernel32::CloseHandle(p R1)'
  ${EndIf}
FunctionEnd

Function un.CheckDeleteErrors
  ${If} ${Errors}
    IfSilent +2
      MessageBox MB_ICONSTOP "$(UninstallBusy)"
    SetErrorLevel 5
    Abort
  ${EndIf}
FunctionEnd
