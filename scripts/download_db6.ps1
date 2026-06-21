# Downloads a documented SUBSET of Ninapro DB6 (preprocessed) — REAL data, no login required.
# Source: https://ninapro.hevs.ch/instructions/DB6.html  (Palermo et al., IEEE ICORR 2017)
# License: data are publicly available via Ninaweb; cite Palermo et al. 2017.
# Each DB6_s{n}_{a,b}.zip is ~1.28 GB. 'a' = first 5 sessions, 'b' = last 5 (10 sessions / 5 days).
$ErrorActionPreference = 'Continue'
$dst  = 'E:\PREHEND\data\ninapro_db6'
$base = 'https://ninapro.hevs.ch/files/DB6_Preproc'
New-Item -ItemType Directory -Force -Path $dst | Out-Null

# Subjects 1-3, both halves => 3 subjects x 10 sessions of REAL multi-session sEMG.
$files = @(
  'DB6_s1_a.zip','DB6_s1_b.zip',
  'DB6_s2_a.zip','DB6_s2_b.zip',
  'DB6_s3_a.zip','DB6_s3_b.zip'
)
foreach ($f in $files) {
  $out = Join-Path $dst $f
  Write-Output ("[{0}] START {1}" -f (Get-Date -Format HH:mm:ss), $f)
  # -C - resumes partial; --retry survives transient drops; -L follows redirects.
  curl.exe -L -C - --retry 4 --retry-delay 5 --fail-with-body -o $out "$base/$f"
  if (Test-Path $out) {
    Write-Output ("[{0}] DONE  {1}  bytes={2}" -f (Get-Date -Format HH:mm:ss), $f, (Get-Item $out).Length)
  } else {
    Write-Output ("[{0}] FAIL  {1}" -f (Get-Date -Format HH:mm:ss), $f)
  }
}
Write-Output "ALL_DB6_DONE"
