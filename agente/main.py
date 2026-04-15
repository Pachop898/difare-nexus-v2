import os
import glob
from datetime import datetime
from agente import analizar
from generar_pdfs import cargar_todos_excels, generar_pdf_ejecutivo, generar_pdf_difare
from dotenv import load_dotenv

load_dotenv()

def enviar_pdfs_outlook(reporte_ia, pdf_ejecutivo, pdf_difare):
    import win32com.client
    outlook = win32com.client.Dispatch("Outlook.Application")
    correo = outlook.CreateItem(0)
    correo.To = os.getenv("CORREO_DESTINO")
    correo.Subject = f"Reporte Semanal IA - {os.getenv('NOMBRE_CLIENTE')} - {datetime.now().strftime('%d/%m/%Y')}"
    correo.Body = f"""
Estimado Equipo Comercial,

El agente DIFARE NEXUS proceso los datos de {os.getenv('NOMBRE_CLIENTE')} y genero 2 reportes:

REPORTE_EJECUTIVO.PDF
Resumen gerencial completo: tendencias, top marcas, ventas por provincia.

REPORTE_DIFARE.PDF
Oportunidades de disponibilidad para Farmacias propias, canal distribucion y alertas de stock bodega.

RESUMEN:
{'='*50}
{reporte_ia[:800]}
{'='*50}

Generado automaticamente por Agente IA - {datetime.now().strftime('%d/%m/%Y %H:%M')}
"""
    correo.Attachments.Add(os.path.abspath(pdf_ejecutivo))
    correo.Attachments.Add(os.path.abspath(pdf_difare))
    correo.Send()
    print(f"Correo enviado a {os.getenv('CORREO_DESTINO')}")

def procesar():
    print("Agente IA de Excel - Iniciando...")

    print("Cargando archivos Excel...")
    df = cargar_todos_excels("excels")
    print(f"Total filas cargadas: {len(df):,}")

    print("Analizando con Claude AI...")
    reporte_ia = analizar("excels")

    if not reporte_ia:
        reporte_ia = "Analisis completado. Ver datos detallados en las secciones del reporte."

    fecha = datetime.now().strftime("%Y%m%d_%H%M")
    pdf_ejecutivo = f"reportes/reporte_ejecutivo_{fecha}.pdf"
    pdf_difare = f"reportes/reporte_difare_{fecha}.pdf"

    print("Generando PDF Ejecutivo...")
    generar_pdf_ejecutivo(df, reporte_ia, pdf_ejecutivo, "excels")

    print("Generando PDF DIFARE Farmacias...")
    generar_pdf_difare(df, pdf_difare, "excels")

    print("Enviando por Outlook...")
    enviar_pdfs_outlook(reporte_ia, pdf_ejecutivo, pdf_difare)

    print("Todo listo!")

if __name__ == "__main__":
    procesar()
