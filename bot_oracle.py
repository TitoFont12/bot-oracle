"""
Bot que pide una máquina "Always Free" de Oracle cada minuto hasta conseguirla.
Solo pide las dos formas gratuitas. Cuando la consigue avisa al móvil (ntfy) y se apaga solo.
Los datos de acceso vienen de los secretos del repositorio (nunca se imprimen).
"""
import os
import subprocess
import time

import oci
import requests

NOMBRE = "escaner"
CLAVE_SSH_PUBLICA = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPlRgBtoCUaMWW9AL4uxO0cnI1XkeFUXmE5bucOXW59+ "
                     "escaner-oracle")
FORMAS = [("VM.Standard.A1.Flex", {"ocpus": 1, "memory_in_gbs": 6}),
          ("VM.Standard.E2.1.Micro", None)]
MINUTOS = int(os.environ.get("MINUTOS", "340"))     # cuánto dura cada vuelta del bot (GitHub corta a las 6 h)
PAUSA = 150                                          # segundos entre intentos (Oracle no deja más seguido)


def log(txt):
    print(time.strftime("%d/%m %H:%M:%S"), txt, flush=True)


def avisar(titulo, texto):
    tema = os.environ.get("NTFY_TEMA")
    if tema:
        try:
            requests.post(f"https://ntfy.sh/{tema}", data=texto.encode("utf-8"),
                          headers={"Title": titulo.encode("utf-8"), "Priority": "high", "Tags": "cloud"},
                          timeout=30)
        except Exception:
            pass


def apagar_bot():
    subprocess.run(["gh", "workflow", "disable", "bot.yml"], capture_output=True)


def ya_existe(comp_client, comp):
    return any(i.lifecycle_state not in ("TERMINATED", "TERMINATING")
               for i in comp_client.list_instances(comp, display_name=NOMBRE).data)


def main():
    cfg = oci.config.from_file()
    comp = cfg["tenancy"]
    cc = oci.core.ComputeClient(cfg)
    net = oci.core.VirtualNetworkClient(cfg)

    if ya_existe(cc, comp):
        log("la máquina ya existe: el bot se apaga")
        apagar_bot()
        return "hecho"

    ad = oci.identity.IdentityClient(cfg).list_availability_domains(comp).data[0].name
    vcn = next(v for v in net.list_vcns(comp).data if v.display_name == "vcn-escaner")
    subred = next(s for s in net.list_subnets(comp, vcn_id=vcn.id).data if s.lifecycle_state == "AVAILABLE")
    imagenes = {}
    for forma, _ in FORMAS:
        imgs = cc.list_images(comp, operating_system="Canonical Ubuntu", operating_system_version="24.04",
                              shape=forma, sort_by="TIMECREATED", sort_order="DESC").data
        imagenes[forma] = ([i for i in imgs if "Minimal" not in i.display_name] or imgs)[0].id

    fin = time.time() + MINUTOS * 60
    intentos = 0
    while time.time() < fin:
        # una sola petición cada 2,5 min, alternando las dos máquinas (más seguido Oracle contesta "demasiados intentos")
        for forma, tam in [FORMAS[intentos % len(FORMAS)]]:
            det = oci.core.models.LaunchInstanceDetails(
                availability_domain=ad, compartment_id=comp, display_name=NOMBRE, shape=forma,
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(**tam) if tam else None,
                source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=imagenes[forma]),
                create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=subred.id, assign_public_ip=True),
                metadata={"ssh_authorized_keys": CLAVE_SSH_PUBLICA})
            try:
                cc.launch_instance(det)
                log(f"¡CONSEGUIDA! {forma}")
                avisar("☁️ ¡Servidor de Oracle conseguido!",
                       f"Máquina {forma}. En menos de 1 h se instala el escáner solo (o díselo a Claude).")
                apagar_bot()
                return "hecho"
            except oci.exceptions.ServiceError as e:
                if e.status == 429:
                    log(f"{forma}: demasiados intentos, espero 2 min")
                    time.sleep(120)
                elif e.status >= 500 or "capacity" in (e.message or "").lower():
                    pass                                 # sin hueco: lo normal
                else:
                    log(f"{forma}: error {e.status} {e.code}")
                    if e.status in (400, 401, 404) and "limit" not in (e.message or "").lower():
                        avisar("⚠️ Bot de Oracle: error", f"Error {e.status} {e.code}. Díselo a Claude.")
                        return "error"
            except Exception as e:
                log(f"fallo de conexión ({type(e).__name__}), sigo")
        intentos += 1
        if intentos % 30 == 0:
            log(f"{intentos} intentos, sin hueco todavía")
        time.sleep(PAUSA)
    log(f"fin de esta vuelta: {intentos} intentos sin hueco")
    return "seguir"


if __name__ == "__main__":
    resultado = main()
    if resultado == "seguir":
        # lanza la siguiente vuelta del bot para que no haya huecos sin intentar
        subprocess.run(["gh", "workflow", "run", "bot.yml"], capture_output=True)
