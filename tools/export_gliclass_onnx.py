#!/usr/bin/env python3
"""
tools/export_gliclass_onnx.py

Tool di esportazione e quantizzazione INT8 per GLiClass / decision-model System 1
adatto al deployment in Neon Rift con ONNX Runtime Web.

Requisiti (ambiente di build/esportazione):
    pip install "gliclass>=0.1.0" "transformers>=4.40.0" "optimum[onnxruntime]" onnx onnxruntime

Uso:
    python tools/export_gliclass_onnx.py --model knowledgator/gliclass-edge-v3.0 --output-dir models/gliclass
"""

import argparse
import os
import sys

def parse_args():
    parser = argparse.ArgumentParser(description="Esporta e quantizza un modello GLiClass in formato ONNX INT8.")
    parser.add_argument("--model", type=str, default="knowledgator/gliclass-edge-v3.0",
                        help="Hugging Face repo id del modello GLiClass (default: knowledgator/gliclass-edge-v3.0)")
    parser.add_argument("--output-dir", type=str, default="models/gliclass",
                        help="Cartella di destinazione per i file ONNX e configurazione")
    parser.add_argument("--quantize", action="store_true", default=True,
                        help="Applica quantizzazione dinamica INT8 per CPU/WASM (default: True)")
    return parser.parse_args()

def main():
    args = parse_args()
    print(f"=== GLiClass ONNX Exporter per Neon Rift ===")
    print(f"Modello sorgente : {args.model}")
    print(f"Cartella output  : {args.output_dir}")
    print(f"Quantizzazione   : {'INT8 dinamica' if args.quantize else 'No (FP32)'}")

    os.makedirs(args.output_dir, exist_ok=True)

    try:
        from transformers import AutoTokenizer
        print("[1/3] Scaricamento e salvataggio tokenizer...")
        tokenizer = AutoTokenizer.from_pretrained(args.model)
        tokenizer.save_pretrained(args.output_dir)
        print(" -> Tokenizer salvato con successo.")
    except Exception as e:
        print(f"[!] Errore nel salvataggio del tokenizer: {e}")
        sys.exit(1)

    try:
        from onnxruntime.quantization import quantize_dynamic, QuantType
        print("[2/3] Verifica ed esportazione modello ONNX...")
        # Nota: L'esportazione diretta può essere effettuata tramite optimum o script ONNX di huggingface
        print("Per completare l'esportazione automatica da HF Hub è consigliato eseguire:")
        print(f"  optimum-cli export onnx --model {args.model} {args.output_dir}/raw_onnx")
        
        raw_model_path = os.path.join(args.output_dir, "raw_onnx", "model.onnx")
        quant_model_path = os.path.join(args.output_dir, "model_int8.onnx")
        
        if os.path.exists(raw_model_path) and args.quantize:
            print(f"[3/3] Applicazione quantizzazione INT8 su {raw_model_path} -> {quant_model_path}...")
            quantize_dynamic(
                model_input=raw_model_path,
                model_output=quant_model_path,
                weight_type=QuantType.QInt8
            )
            print(f" -> Modello INT8 pronto in: {quant_model_path}")
        else:
            print(" -> Se hai già scaricato un checkpoint ONNX da Hugging Face, posizionalo in: " + args.output_dir)
    except ImportError:
        print("[!] Moduli onnxruntime/optimum non presenti in questo ambiente Python.")
        print("    Questo script è pronto per essere eseguito su una macchina di build o ambiente con dipendenze ML.")

    # Genera metadati di configurazione tattica per Neon Rift
    manifest_path = os.path.join(args.output_dir, "stances_manifest.json")
    import json
    stances_manifest = {
        "model": args.model,
        "format": "onnx-web",
        "labels": [
            {"id": "aggressive", "description": "High player danger or player vulnerable, push offensive attack"},
            {"id": "defensive", "description": "High asteroid density, retreat to safe perimeter, shield focus"},
            {"id": "hunter", "description": "Player has low lives or active Hunter Becomes Hunted event"},
            {"id": "scavenge", "description": "Valuable power-up like life or laser spawned in arena, move to collect"},
            {"id": "sniper", "description": "Keep safe distance and fire aligned precision shots"}
        ]
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(stances_manifest, f, indent=2)
    print(f" -> Generato manifest delle stance in: {manifest_path}")

if __name__ == "__main__":
    main()
