#  enter the certificate thumbprint for the server
$ssl_thumbprint = <REPLACE_WITH_THUMBPRINT>

netsh http delete sslcert ipport=0.0.0.0:443

netsh http show sslcert

netsh http add sslcert ipport=0.0.0.0:443 certhash=$ssl_thumbprint appid='{eea9431a-a3d4-4c9b-9f9a-b83916c11c67}'