param (
    [Parameter(Mandatory=$true)]
    [string]$OnePath,
    
    [Parameter(Mandatory=$true)]
    [string]$DocxPath
)

$ErrorActionPreference = "Stop"

try {
    # Resolve absolute paths
    $oneAbsPath = [System.IO.Path]::GetFullPath($OnePath)
    $docxAbsPath = [System.IO.Path]::GetFullPath($DocxPath)
    
    Write-Host "Connecting to OneNote.Application COM object..."
    $OneNote = New-Object -ComObject OneNote.Application
    
    Write-Host "Opening OneNote Section: $oneAbsPath"
    $SectionId = ""
    # 3 = cftSection (CreateFileType)
    $OneNote.OpenHierarchy($oneAbsPath, "", [ref]$SectionId, 3)
    
    Write-Host "Exporting Section to Word Docx: $docxAbsPath"
    # 5 = pfWord (PublishFormat)
    $OneNote.Publish($SectionId, $docxAbsPath, 5, "")
    
    Write-Host "Export successful!"
    exit 0
} catch {
    Write-Error $_
    exit 1
}
