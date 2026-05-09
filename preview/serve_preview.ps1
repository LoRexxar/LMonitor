 $root = "d:\program\LMonitor"
 $port = 8123
 if ($args.Length -ge 1) {
   $maybe = 0
   if ([int]::TryParse([string]$args[0], [ref]$maybe) -and $maybe -gt 0) {
     $port = $maybe
   }
 }
 $prefix = "http://localhost:$port/"
 
 $listener = New-Object System.Net.HttpListener
 $listener.Prefixes.Add($prefix) | Out-Null
 $listener.Start()
 Write-Output "Serving $root at $prefix"
 
 function Get-ContentType([string]$ext) {
   switch ($ext.ToLowerInvariant()) {
     ".html" { return "text/html; charset=utf-8" }
     ".css" { return "text/css; charset=utf-8" }
     ".js" { return "application/javascript; charset=utf-8" }
     ".json" { return "application/json; charset=utf-8" }
     ".png" { return "image/png" }
     ".jpg" { return "image/jpeg" }
     ".jpeg" { return "image/jpeg" }
     ".svg" { return "image/svg+xml" }
     default { return "application/octet-stream" }
   }
 }
 
 while ($listener.IsListening) {
   $ctx = $listener.GetContext()
   $req = $ctx.Request
   $res = $ctx.Response
 
   try {
     $path = [Uri]::UnescapeDataString($req.Url.AbsolutePath)
     if ([string]::IsNullOrWhiteSpace($path) -or $path -eq "/") {
      $path = "/preview/portal_current_preview.html"
     }
 
     $safe = $path.TrimStart("/") -replace "/", "\"
     $full = [IO.Path]::GetFullPath((Join-Path $root $safe))
     $rootFull = [IO.Path]::GetFullPath($root)
 
     if (-not $full.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
       $res.StatusCode = 403
       $bytes = [Text.Encoding]::UTF8.GetBytes("Forbidden")
       $res.OutputStream.Write($bytes, 0, $bytes.Length)
       continue
     }
 
     if (-not (Test-Path $full -PathType Leaf)) {
       $res.StatusCode = 404
       $bytes = [Text.Encoding]::UTF8.GetBytes("Not Found")
       $res.OutputStream.Write($bytes, 0, $bytes.Length)
       continue
     }
 
     $ext = [IO.Path]::GetExtension($full)
     $res.ContentType = Get-ContentType $ext
     $bytes = [IO.File]::ReadAllBytes($full)
     $res.ContentLength64 = $bytes.Length
     $res.OutputStream.Write($bytes, 0, $bytes.Length)
   } catch {
     $res.StatusCode = 500
     $bytes = [Text.Encoding]::UTF8.GetBytes("Server Error")
     $res.OutputStream.Write($bytes, 0, $bytes.Length)
   } finally {
     $res.OutputStream.Close()
     $res.Close()
   }
 }
