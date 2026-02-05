from flask import Flask, jsonify
import boto3
import gzip
import io
import pandas as pd
import joblib
import os
from collections import defaultdict

app = Flask(__name__)

# MODELİ YÜKLE
model = joblib.load("model_vpc.pkl")
feature_names = ["duration", "packets", "bytes", "packets_per_sec", "bytes_per_sec"]

# AWS Ayarları
s3 = boto3.client("s3", region_name='us-east-1')
sns = boto3.client("sns", region_name="us-east-1")

BUCKET = "log-bucket-final-project"
LOG_PREFIX = "AWSLogs/047824595614/vpcflowlogs/us-east-1/2025/05/31"
SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:047824596341:projeTopic"

@app.route('/analyze', methods=['GET'])
def analyze_logs():
    try:
        response = s3.list_objects_v2(Bucket=BUCKET, Prefix=LOG_PREFIX)
        files = sorted(response.get('Contents', []), key=lambda x: x['LastModified'], reverse=True)

        if not files:
            return jsonify({"error": "Log dosyası bulunamadı"}), 404

        latest_file_key = files[0]['Key']
        print("En güncel dosya:", latest_file_key)

        gz_obj = s3.get_object(Bucket=BUCKET, Key=latest_file_key)
        with gzip.GzipFile(fileobj=io.BytesIO(gz_obj['Body'].read())) as f:
            lines = f.read().decode('utf-8').splitlines()

        predictions = []
        port_scan_ips = defaultdict(set)
        ddos_counter = defaultdict(int)
        sql_injection_detected = set()
        print("Log Dosyası Analiz Ediliyor")

        for line in lines:
            parts = line.split()
            try:
                src_ip = parts[3]
                dst_ip = parts[4]
                dst_port = parts[5]
                start = int(parts[10])
                end = int(parts[11])
                duration = max(end - start, 1)
                packets = int(parts[8])
                bytes_count = int(parts[9])
                pps = packets / duration
                bps = bytes_count / duration

                # Port Scan
                port_scan_ips[src_ip].add(dst_port)

                # DDoS
                ddos_counter[dst_ip] += 1

                # SQL Injection (şüpheli karakter taraması)
                suspicious_patterns = ["'", "--", " or ", " OR ", "%27", "1=1", "\"", "\\", "admin", "login"]
                if any(p in line.lower() for p in suspicious_patterns):
                    sql_injection_detected.add(src_ip)

                # AI Prediction
                data = pd.DataFrame([[duration, packets, bytes_count, pps, bps]], columns=feature_names)
                pred = model.predict(data)[0]
                score = model.predict_proba(data)[0][1]



                if str(pred).lower() != "normal" and score >= 0.5:
                    predictions.append({
                        "src": src_ip,
                        "dst": dst_ip,
                        "prediction": str(pred).upper(),
                        "threat_score": round(score * 100, 2),
                        "method": "AI"
                    })

                    # SNS
                    message = f"""
AI Tabanlı IDS Saldırı Tespiti!
Saldırı Türü: {pred}
Kaynak: {src_ip}
Hedef: {dst_ip}
Tehdit Skoru: {round(score * 100, 2)}
"""
                    sns.publish(TopicArn=SNS_TOPIC_ARN, Message=message, Subject=" AI Saldırı Alarmı")

            except Exception as e:
                continue

        # Port Scan
        for ip, ports in port_scan_ips.items():
            if len(ports) >= 30:
                predictions.append({
                    "src": ip,
                    "prediction": "PORTSCAN",
                    "port_count": len(ports),
                    "method": "SIGNATURE"
                })

                message = f"""
Port Scan Tespiti
Kaynak IP: {ip}
Taradığı Port Sayısı: {len(ports)}
Not: 30+ port taraması tespit edildi.
"""
                sns.publish(TopicArn=SNS_TOPIC_ARN, Message=message, Subject="Port Scan Alarm")

        #DDoS
        for ip, count in ddos_counter.items():
            if count >= 50:
                predictions.append({
                    "dst": ip,
                    "prediction": "DDOS",
                    "flow_count": count,
                    "method": "SIGNATURE"
                })

                message = f"""
DDoS Tespiti
Hedef IP: {ip}
Bağlantı Sayısı: {count}
Not: 50+ bağlantı aynı hedefe tespit edildi.
"""
                sns.publish(TopicArn=SNS_TOPIC_ARN, Message=message, Subject="DDoS Alarm")

        #SQL Injection
        for ip in sql_injection_detected:
            predictions.append({
                "src": ip,
                "prediction": "SQL_INJECTION",
                "method": "SIGNATURE"
            })

            message = f"""
SQL Injection Tespiti
Kaynak IP: {ip}
Şüpheli karakter dizileri tespit edildi.
Not: SQLi saldırısı ihtimali
"""
            sns.publish(TopicArn=SNS_TOPIC_ARN, Message=message, Subject="SQL Injection Alarm")

        if not predictions:
            return jsonify({"status": "No attack detected "})

        return jsonify({
            "status": "ATTACK DETECTED",
            "count": len(predictions),
            "details": predictions
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
