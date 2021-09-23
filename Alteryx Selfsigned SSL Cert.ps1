$date_now = Get-Date
$extended_date = $date_now.AddYears(3)
$cert = New-SelfSignedCertificate -certstorelocation cert:\localmachine\my -dnsname hostname, loadbalancerDNSname -notafter $extended_date -KeyLength 4096
$pwd = ConvertTo-SecureString -String ‘addyoursecurepasswordhere’ -Force -AsPlainText
$path = ‘cert:\localMachine\my\’ + $cert.thumbprint
Export-PfxCertificate -cert $path -FilePath C:\Users\Administrator\path\to\self-signed-cert.pfx -Password $pwd
# Needs testing but could add this on the end to bind the above cert to 443
netsh http add sslcert ipport=0.0.0.0:443 certhash=‎$cert.thumbprint appid={eea9431a-a3d4-4c9b-9f9a-b83916c11c67}
$SourceStore = New-Object  -TypeName System.Security.Cryptography.X509Certificates.X509Store  -ArgumentList my, LocalMachine
$SourceStore.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
$copycert = $SourceStore.Certificates | Where-Object  -FilterScript {
    $_.subject -like 'EC2AMAZ*'
}
$DestStore = New-Object  -TypeName System.Security.Cryptography.X509Certificates.X509Store  -ArgumentList root, LocalMachine
$DestStore.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
$DestStore.Add($copycert)
$SourceStore.Close()
$DestStore.Close()