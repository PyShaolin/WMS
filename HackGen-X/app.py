from flask import Flask, render_template, request, jsonify
from pymongo import MongoClient
from bson import ObjectId, json_util
import json
from datetime import datetime, timedelta
import ast

app = Flask(__name__)

@app.route('/assets/<path:filename>')
def custom_static(filename):
    return send_from_directory('assets', filename)

# MongoDB Connection
client = MongoClient("mongodb://localhost:27017/")
db = client.warehouse_db

def parse_json(data):
    return json.loads(json_util.dumps(data))

def parse_capacity(capacity):
    """Parse capacity whether it's stored as string or dict"""
    if isinstance(capacity, str):
        try:
            return ast.literal_eval(capacity)
        except (ValueError, SyntaxError):
            return {'length': 0, 'width': 0, 'height': 0}
    return capacity

@app.route('/')
def dashboard():
    """Main dashboard route"""
    try:
        items = list(db.inventory.find().sort("entry_date", -1).limit(10))
        total_items = db.inventory.count_documents({})
        zones = db.warehouse_layout.distinct("zone_id")
        
        bins = list(db.warehouse_layout.find())
        total_capacity = 0
        used_capacity = 0
        
        for b in bins:
            capacity = parse_capacity(b['capacity'])
            vol = capacity['length'] * capacity['width'] * capacity['height']
            total_capacity += vol
            used_capacity += vol * b.get('current_utilization', 0)
        
        utilization = (used_capacity/total_capacity)*100 if total_capacity > 0 else 0
        
        return render_template('index.html', 
                            items=parse_json(items),
                            total_items=total_items,
                            zones=zones,
                            utilization=f"{utilization:.1f}%")
    except Exception as e:
        return f"Error loading dashboard: {str(e)}", 500

@app.route('/api/item', methods=['POST'])
def get_item():
    """Get item details endpoint"""
    try:
        data = request.get_json()
        if not data or 'item_name' not in data:
            return jsonify({"status": "error", "message": "Missing item_name parameter"}), 400
            
        item_name = data['item_name']
        item = db.inventory.find_one({"item_name": item_name})
        if not item:
            return jsonify({"status": "error", "message": "Item not found"}), 404
        
        loc_parts = item['current_location'].split('-')
        bin_data = db.warehouse_layout.find_one({
            "zone_id": loc_parts[0],
            "rack_id": loc_parts[1],
            "bin_id": loc_parts[2]
        })
        
        if bin_data and 'capacity' in bin_data:
            bin_data['capacity'] = parse_capacity(bin_data['capacity'])
        
        movement_history = list(db.movement_logs.find({"item_id": item["item_id"]}).sort("timestamp", -1).limit(5))
        
        item_data = parse_json(item)
        item_data['bin_details'] = parse_json(bin_data) if bin_data else None
        item_data['movement_history'] = parse_json(movement_history)
        return jsonify({"status": "success", "data": item_data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/warehouse/stats')
def get_warehouse_stats():
    """Warehouse statistics endpoint"""
    try:
        zones = []
        for zone in db.warehouse_layout.distinct("zone_id"):
            zone_bins = list(db.warehouse_layout.find({"zone_id": zone}))
            zone_cap = 0
            zone_used = 0
            
            for b in zone_bins:
                capacity = parse_capacity(b['capacity'])
                vol = capacity['length'] * capacity['width'] * capacity['height']
                zone_cap += vol
                zone_used += vol * b.get('current_utilization', 0)
            
            utilization = (zone_used/zone_cap)*100 if zone_cap > 0 else 0
            zones.append({
                "name": zone,
                "utilization": f"{utilization:.1f}",
                "bins": len(zone_bins)
            })
        
        pipeline = [
            {"$group": {"_id": "$category", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ]
        categories = list(db.inventory.aggregate(pipeline))
        
        expiring_soon = db.inventory.count_documents({
            "expiry_date": {
                "$lt": datetime.now() + timedelta(days=7),
                "$gte": datetime.now()
            }
        })
        
        return jsonify({
            "zones": zones,
            "categories": parse_json(categories),
            "total_items": db.inventory.count_documents({}),
            "expiring_soon": expiring_soon
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/item/add', methods=['POST'])
def add_item():
    """Add new item endpoint"""
    try:
        if not request.is_json:
            return jsonify({"status": "error", "message": "Request must be JSON"}), 400
            
        form_data = request.get_json()
        
        required_fields = ['item_id', 'item_name', 'category', 'dimensions', 'weight', 'current_location']
        for field in required_fields:
            if field not in form_data:
                return jsonify({"status": "error", "message": f"Missing required field: {field}"}), 400
        
        new_item = {
            "item_id": form_data['item_id'],
            "item_name": form_data['item_name'],
            "category": form_data['category'],
            "dimensions": {
                "length": float(form_data['dimensions']['length']),
                "width": float(form_data['dimensions']['width']),
                "height": float(form_data['dimensions']['height'])
            },
            "weight": float(form_data['weight']),
            "fragility": bool(form_data.get('fragility', False)),
            "expiry_date": datetime.fromisoformat(form_data['expiry_date']) if form_data.get('expiry_date') else None,
            "current_location": form_data['current_location'],
            "entry_date": datetime.now()
        }
        
        db.inventory.insert_one(new_item)
        
        db.movement_logs.insert_one({
            "item_id": new_item['item_id'],
            "timestamp": datetime.now(),
            "movement_type": "in",
            "location": new_item['current_location'],
            "order_id": "SYSTEM_ADD"
        })
        
        return jsonify({"status": "success", "message": "Item added successfully"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/item/delete', methods=['POST'])
def delete_item():
    """Delete item endpoint"""
    try:
        # Check for both form data and JSON payload
        if request.is_json:
            data = request.get_json()
            item_id = data.get('item_id')
        else:
            item_id = request.form.get('item_id')
            
        if not item_id:
            return jsonify({"status": "error", "message": "Missing item_id parameter"}), 400
            
        result = db.inventory.delete_one({"_id": ObjectId(item_id)})
        
        if result.deleted_count == 0:
            return jsonify({"status": "error", "message": "Item not found"}), 404
            
        return jsonify({"status": "success", "message": "Item deleted"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)