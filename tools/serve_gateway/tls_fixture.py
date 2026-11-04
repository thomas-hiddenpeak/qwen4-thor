"""Private test PKI; explicit certificate dates, no sockets or network."""
import datetime
from pathlib import Path
import secrets
from tools.serve_gateway import gateway as g,tls

HOST='q4t.internal.test'


def stamp(epoch):
    return datetime.datetime.fromtimestamp(epoch,datetime.timezone.utc).strftime('%Y%m%d%H%M%SZ')


def request(root,name):
    key,csr=root/(name+'.key'),root/(name+'.csr')
    tls.command(['req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1','-nodes',
                 '-subj','/CN='+name,'-keyout',str(key),'-out',str(csr)])
    key.chmod(0o600)
    return key,csr


def ca(root):
    root=Path(root);root.mkdir(mode=0o700)
    key,csr=request(root,'root')
    cert=root/'root.pem'
    tls.command(['req','-x509','-key',str(key),'-in',str(csr),'-days','60',
        '-out',str(cert),'-addext','basicConstraints=critical,CA:TRUE',
        '-addext','keyUsage=critical,keyCertSign,cRLSign','-addext','subjectKeyIdentifier=hash'])
    return cert,key


def issue(root,name,issuer,begin,end,*,is_ca=False,purpose='serverAuth',host=HOST):
    root=Path(root);root.mkdir(mode=0o700)
    key,csr=request(root,name);cert=root/(name+'.pem')
    g.save(root/'index',b'');g.save(root/'serial',(secrets.token_hex(16)+'\n').encode())
    cert_ca,key_ca=issuer
    extension=('basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n'
        if is_ca else 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'
        f'extendedKeyUsage={purpose}\nsubjectAltName=DNS:{host}\n')
    config=(f'[ca]\ndefault_ca=issuer\n[issuer]\ndatabase={root}/index\nnew_certs_dir={root}\n'
        f'certificate={cert_ca}\nprivate_key={key_ca}\nserial={root}/serial\ndefault_md=sha256\n'
        'policy=names\nx509_extensions=extensions\nunique_subject=no\n[names]\ncommonName=supplied\n'
        '[extensions]\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n'+extension)
    g.save(root/'ca.cnf',config.encode())
    tls.command(['ca','-batch','-notext','-config',str(root/'ca.cnf'),'-in',str(csr),
        '-out',str(cert),'-startdate',stamp(begin),'-enddate',stamp(end)])
    return cert,key


def chain(root,leaf,intermediate):
    result=Path(root)/'chain.pem'
    g.save(result,leaf.read_bytes()+intermediate.read_bytes())
    return result
