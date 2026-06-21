# Downloads a documented SUBSET of GRABMyo v1.1.0 — REAL data, openly accessible (CC-BY 4.0).
# Source: https://physionet.org/content/grabmyo/1.1.0/  (Pradhan et al., Sci Data 2022)
# Public S3 mirror (no credentials): s3://physionet-open/grabmyo/1.1.0/
# Layout: Session{1,2,3}/session{i}_participant{j}/session{i}_participant{j}_gesture{k}_trial{t}.{hea,dat}
# Sessions correspond to days 1, 8, 29.
$ErrorActionPreference = 'Continue'
$dst  = 'E:\PREHEND\data\grabmyo\1.1.0'
$base = 'https://physionet-open.s3.amazonaws.com/grabmyo/1.1.0'
New-Item -ItemType Directory -Force -Path $dst | Out-Null

# Metadata / license files (small, always fetch).
foreach ($m in 'LICENSE.txt','SHA256SUMS.txt','GestureList.JPG','MotionSequence.txt','RECORDS') {
  curl.exe -s -L --fail -o (Join-Path $dst $m) "$base/$m"
}

# Subset: 4 participants x 3 sessions. Gestures 1,6,11 are mapped to commands; 17 is rest baseline.
$participants = 1..4
$sessions     = 1..3
$gestures     = @(1,6,11,17)
$trials       = 1..7
$count = 0
foreach ($s in $sessions) {
  foreach ($p in $participants) {
    $rel = "Session$s/session${s}_participant${p}"
    $od  = Join-Path $dst $rel
    New-Item -ItemType Directory -Force -Path $od | Out-Null
    foreach ($g in $gestures) {
      foreach ($t in $trials) {
        $rec = "session${s}_participant${p}_gesture${g}_trial${t}"
        foreach ($ext in 'hea','dat') {
          $out = Join-Path $od "$rec.$ext"
          if (-not (Test-Path $out)) {
            curl.exe -s -L --fail --retry 3 -o $out "$base/$rel/$rec.$ext"
            $count++
          }
        }
      }
    }
    Write-Output ("[{0}] session{1} participant{2} fetched" -f (Get-Date -Format HH:mm:ss), $s, $p)
  }
}
Write-Output ("ALL_GRABMYO_DONE files_attempted={0}" -f $count)
