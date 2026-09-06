Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

Name "COWMATA ${VERSION}"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\COWMATA-${VERSION}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32
SetDatablockOptimize on
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "3.1.0.0"
VIAddVersionKey "ProductName" "COWMATA Annotator"
VIAddVersionKey "FileDescription" "COWMATA Offline Setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "COWMATA contributors"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${PACKAGE}\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "COWMATA requires Windows 10/11 x64."
    Abort
  ${EndIf}
FunctionEnd

; The installer never merges into existing folders or updates raw data.
Function .onVerifyInstDir
  StrLen $0 $INSTDIR
  ${If} $0 < 10
    SetErrorLevel 2
    Abort
  ${EndIf}
  ${If} $0 > 100
    SetErrorLevel 2
    Abort
  ${EndIf}
  ClearErrors
  FindFirst $0 $1 "$INSTDIR\*"
  ${DoUntil} ${Errors}
    ${If} $1 != "."
    ${AndIf} $1 != ".."
      FindClose $0
      SetErrorLevel 2
      Abort
    ${EndIf}
    FindNext $0 $1
  ${Loop}
  FindClose $0
  ClearErrors
FunctionEnd

Section "COWMATA" Main
  ; Also enforce the non-overwrite rule for silent installs.
  Call .onVerifyInstDir
  ClearErrors
  SetOutPath "$INSTDIR"
  !include "${INSTALL_LIST}"
  IfErrors 0 +3
    MessageBox MB_ICONSTOP "Installation failed. Original data was not changed."
    Abort
  FileOpen $0 "$INSTDIR\COWMATA.install-id" w
  FileWrite $0 "COWMATA-${VERSION}"
  FileClose $0
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\COWMATA ${VERSION}"
  CreateShortcut "$SMPROGRAMS\COWMATA ${VERSION}\COWMATA.lnk" "$INSTDIR\COWMATA.exe"
  CreateShortcut "$SMPROGRAMS\COWMATA ${VERSION}\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "DisplayName" "COWMATA ${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "UninstallString" '$"$INSTDIR\Uninstall.exe$"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}" "InstallLocation" "$INSTDIR"
SectionEnd

Section "Uninstall"
  FileOpen $0 "$INSTDIR\COWMATA.install-id" r
  IfErrors unsafe
  FileRead $0 $1
  FileClose $0
  StrCmp $1 "COWMATA-${VERSION}" 0 unsafe
  ; Generated exact Delete paths, then empty-directory RMDir only. No /r.
  !include "${UNINSTALL_LIST}"
  Delete "$INSTDIR\COWMATA.install-id"
  Delete "$INSTDIR\Uninstall.exe"
  SetOutPath "$TEMP"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\COWMATA ${VERSION}\COWMATA.lnk"
  Delete "$SMPROGRAMS\COWMATA ${VERSION}\Uninstall.lnk"
  RMDir "$SMPROGRAMS\COWMATA ${VERSION}"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\COWMATA-${VERSION}"
  Goto done
  unsafe:
    MessageBox MB_ICONSTOP "Installation identity does not match. No files were removed."
    SetErrorLevel 2
    Abort
  done:
SectionEnd
