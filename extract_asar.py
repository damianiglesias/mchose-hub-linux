#!/usr/bin/env python3
"""Extraer archivo ASAR de Electron (formato custom con header ZIP) usando zipfile."""

import zipfile
import sys
from pathlib import Path


def extract_custom_asar(asar_path: str, output_dir: str):
    """
    Extrae un archivo ASAR estándar de Electron.
    
    Los archivos ASAR son simplemente archivos ZIP con nombres de archivos modificados (puntos reemplazados por barras).
    Usamos zipfile directamente que maneja esto automáticamente.
    """
    asar_path = Path(asar_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Extrayendo: {asar_path}")
    print(f"Saliendo en: {output_dir}")
    
    try:
        with zipfile.ZipFile(asar_path, 'r') as zf:
            # Listamos los primeros 20 archivos para verificar formato
            files = zf.namelist()[:30]
            print("\nPrimeros 30 archivos en el ASAR:")
            for f in files:
                print(f"  {f}")
            
            if len(files) < 10:
                print("ERROR: El archivo parece estar corrupto o no es un ZIP válido")
                return False
            
            # Extraemos todos los archivos (reemplazando puntos con barras si son nombres de ruta)
            zf.extractall(output_dir)
            
        print(f"\n¡Extracción completada! {len(files)} archivos extraídos.")
        
    except zipfile.BadZipFile as e:
        print(f"ERROR: No es un archivo ZIP válido - {e}")
        return False
    except Exception as e:
        print(f"ERROR inesperado: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True


def search_for_hid_commands(output_dir: str, patterns=None):
    """Buscar comandos HID en los archivos extraídos."""
    output_path = Path(output_dir)
    
    if not patterns:
        # Patrones comunes de comandos HID
        patterns = [
            r'setDpi\s*\(',
            r'hidReport',
            r'hidFeatureReport',
            r'deviceWrite.*0x[0-9a-fA-F]{2}\s*,\s*0x[0-9a-fA-F]+',
            r'SET_DPI|SET_REPORT_RATE|polling_rate',
        ]
    
    found_files = []
    
    for ext in ['*.js', '*.json']:
        print(f"\nBuscando en archivos {ext}...")
        
        import glob
        files = list(output_path.glob(ext))[:50]  # Limitar a primeros 50
        
        for filepath in files:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                if patterns is None or any(p.search(content) for p in patterns):
                    found_files.append((filepath, len(patterns) if patterns else True))
            
            except Exception as e:
                pass
    
    print(f"\nArchivos encontrados con comandos HID (o sin filtro si no especificado): {len(found_files)}")
    
    # Mostrar los primeros 10 resultados
    for filepath, _ in found_files[:25]:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            print(f"\n{'='*60}")
            print(f"Archivo: {filepath.name} ({len(content)} bytes)")
            print('='*60)
            
            # Mostrar líneas relevantes
            for line in content.split('\n')[:15]:  # Primeras 15 líneas
                if any(p.search(line) for p in patterns):
                    print(f"  {line}")
                    
        except Exception:
            pass
    
    return found_files


if __name__ == '__main__':
    import sys
    
    asar_file = '/home/damian/Downloads/MHUB/resources/app.asar'
    extract_dir = '/tmp/mhub_extracted'
    
    if len(sys.argv) > 1:
        asar_file = sys.argv[1]
    if len(sys.argv) > 2:
        extract_dir = sys.argv[2]
    
    success = extract_custom_asar(asar_file, extract_dir)
    
    if success:
        print("\n" + "="*60)
        print("Buscando comandos HID relevantes...")
        search_for_hid_commands(extract_dir)
