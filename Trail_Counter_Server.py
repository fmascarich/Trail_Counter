
import serial
from flask import Flask, render_template, Response
import datetime
import threading
import os
import schedule
import re
import wiringpi
import time

device_tracking_time = 60*15  # every 15 minutes
summary_log_save_time = 60*15 # every 15 minutes

class scheduler_thread(threading.Thread): 
	def __init__(self, event, serial_obj):
		threading.Thread.__init__(self)
		self.stopped = event
		schedule.every(1).minutes.do(serial_obj.check_for_old)
		schedule.every(5).minutes.do(serial_obj.update_summary)
		#schedule.every(25).seconds.do(serial_obj.reset_esp32)

	def run(self):
		while True:
			schedule.run_pending()
			time.sleep(1)

class serial_thread(threading.Thread): 
	def __init__(self, event, port_name):
		threading.Thread.__init__(self)
		self.serial_port_name = port_name
		self.stopped = event
		self.serial_port = None
		self.reset_esp32()
		self.recent_bt_dict = {}
		self.recent_wifi_dict = {}
		self.recent_bt_count = 0
		self.recent_wifi_count = 0
		self.line_buffer = ""

		self.wifi_reg_ex = re.compile('(<[W\|w]#[A-F\|0-9]{12}>)')
		self.bt_reg_ex = re.compile('(<[B\|b]#([a-f\|0-9]{2}:){5}([a-f\|0-9]{2})>)')

		if not os.path.exists("/home/pi/rpiWebServer/logs/summary_log.csv"):
			with open("/home/pi/rpiWebServer/logs/summary_log.csv", "w+") as outfile:
				outfile.write("Datetime, Bluetooth Count, WiFi Count\n")

		if not os.path.exists("/home/pi/rpiWebServer/logs/complete_log.csv"):
			with open("/home/pi/rpiWebServer/logs/complete_log.csv", "w+") as outfile:
				outfile.write("Datetime, Type, Address\n")

		self.update_summary()
		self.last_summary_datetime = self.get_now_time()

	def run(self):
		counter = 0
		while not self.stopped.wait(0.001):
			if self.serial_port is None:
				self.stopped.wait(1.0)
			elif not self.serial_port.isOpen():
				self.stopped.wait(1.0)
			self.check_serial()

	def check_for_old(self):
		now = self.get_now_time()
		remove_keys = []
		for key in self.recent_bt_dict.keys():
			if(now - self.recent_bt_dict[key]).seconds > device_tracking_time:
				remove_keys.append(key)
		for key in remove_keys:
			print("Removing Key : ", key)
			try:
				del self.recent_bt_dict[key]
			except KeyError as err:
				print(err)
			
		remove_keys = []
		for key in self.recent_wifi_dict.keys():
			if(now - self.recent_wifi_dict[key]).seconds > device_tracking_time:
				remove_keys.append(key)
		for key in remove_keys:
			print("Removing Key : ", key)
			try:
				del self.recent_wifi_dict[key]
			except KeyError as err:
				print(err)

	def check_summary_update(self):
		now  = self.get_now_time()
		if self.last_summary_datetime is None:
			self.update_summary()
			return
		elif(now - self.last_summary_datetime).seconds > (summary_log_save_time):
			self.update_summary()
	
	def update_summary(self):
		print("Updating Summary")
		with open("/home/pi/rpiWebServer/logs/summary_log.csv", 'a') as outfile:
			outfile.write(self.get_now_string())
			outfile.write(",")
			outfile.write(str(self.recent_bt_count))
			outfile.write(",")
			outfile.write(str(self.recent_wifi_count))
			outfile.write('\n')
		self.last_summary_datetime = self.get_now_time()
		self.recent_bt_count = 0
		self.recent_wifi_count = 0

	def add_detection_to_log(self, d_type, address):
		if d_type == 'bt':
			self.recent_bt_count += 1
		elif d_type == 'wifi':
			self.recent_wifi_count += 1
		with open("/home/pi/rpiWebServer/logs/complete_log.csv", 'a') as outfile:
			outfile.write(self.get_now_string())
			outfile.write(",")
			outfile.write(str(d_type))
			outfile.write(",")
			outfile.write(str(address))
			outfile.write('\n')

	def check_serial(self):
		if self.serial_port is None:
			print("SERIAL PORT is NONE")
			return
		try:		
			if self.serial_port.isOpen():
				in_char = self.serial_port.read(1).decode('UTF-8')
				if len(in_char) > 0:
					self.line_buffer += in_char
			else:
				return
		except serial.SerialException as ex:
			print ("Generic Serial Exception")
			if not self.serial_port.isOpen():
				self.open_port()
				return
			print(ex)
		except serial.SerialTimeoutException as ex:
			print ("Serial Timeout Exception")
			print(ex)
		except:
			print ("EXCEPTION READING FROM SERIAL PORT")
		if len(self.line_buffer) > 0:
			if '\n' in self.line_buffer:
				newline_pos = self.line_buffer.find('\n')
				self.line_buffer = self.line_buffer[:newline_pos]
				if '\r' in self.line_buffer:
					return_pos = self.line_buffer.find('\r')
					self.line_buffer = self.line_buffer[:return_pos]
				regex_result = self.wifi_reg_ex.match(self.line_buffer)
				if regex_result:
					self.process_line_buffer(self.line_buffer[regex_result.span()[0]:regex_result.span()[1]])
					self.line_buffer = ""
					return
				regex_result = self.bt_reg_ex.match(self.line_buffer)
				if regex_result:
					self.process_line_buffer(self.line_buffer[regex_result.span()[0]:regex_result.span()[1]])
					self.line_buffer = ""
					return
					
				while self.line_buffer.find('<') != -1 and self.line_buffer.find('>') != -1:
					print("Found message without regex match, discarding buffer : ", self.line_buffer)
					self.line_buffer = self.line_buffer[self.line_buffer.find('>')+1:]
		if len(self.line_buffer) > 255:
			self.line_buffer = self.line_buffer[100:]


	def process_line_buffer(self, line):
		start = line.find('<')
		addr_start = line.find('#')
		end = line.find('>')
		if start == -1 or end == -1 or addr_start == -1:
			return
		# get the type of detection (the first char after the start char)
		detection_type = line[start + 1]
		# get the mac address (between # and > )
		mac_addr = line[addr_start+1:end]
		for c in mac_addr:
			if(not c.isalnum() and c != ':'):
				print("Found invalid char : ", c)
				return
		if detection_type == 'W':
			print("ADDING WIFI : ", mac_addr, " time = ", datetime.datetime.now())
			self.add_wifi(mac_addr)
		elif detection_type == 'B':
			print("ADDING BT : ", mac_addr, " time = ", datetime.datetime.now())
			self.add_bt(mac_addr)
		else:
			print("UNKNOWN DETECTION TYPE")

	def add_bt(self, address):
		if address not in self.recent_bt_dict.keys():
			self.add_detection_to_log("bt", address)
		self.recent_bt_dict[address] = datetime.datetime.now()

	def add_wifi(self, address):
		if address not in self.recent_wifi_dict.keys():
			self.add_detection_to_log("wifi", address)
		self.recent_wifi_dict[address] = self.get_now_time()

	def get_bt_count(self):
		return len(self.recent_bt_dict.keys())

	def get_wifi_count(self):
		return len(self.recent_wifi_dict.keys())

	def get_now_time(self):
		return datetime.datetime.now()

	def get_now_string(self):
		return self.get_now_time().strftime("%Y-%m-%d %H:%M.%S")

	def reset_detections(self):
		self.recent_bt_dict = {}
		self.recent_wifi_dict = {}
		self.recent_bt_count = 0
		self.recent_wifi_count = 0
		with open("/home/pi/rpiWebServer/logs/summary_log.csv", "w+") as outfile:
			outfile.write("Datetime, Bluetooth Count, WiFi Count\n")

		with open("/home/pi/rpiWebServer/logs/complete_log.csv", "w+") as outfile:
			outfile.write("Datetime, Type, Address\n")
	
	def open_port(self):
		self.line_buffer = ""
		self.serial_port = serial.Serial(self.serial_port_name, 9600, timeout=0.1)

	def reset_esp32(self):
		print("Restarting ESP32!!!")
		if self.serial_port is not None:
			self.serial_port.close()
			self.serial_port = None
			time.sleep(1.0)
		self.line_buffer = ""
		wiringpi.digitalWrite(12, 0)
		time.sleep(10.0)
		wiringpi.digitalWrite(12, 1)
		time.sleep(10.0)
		self.line_buffer = ""
		self.open_port()
		print(self.serial_port)

		print("Restarting ESP32 Complete!!!")


app = Flask(__name__)
@app.route("/")
def index():
	now = datetime.datetime.now()
	timeString = now.strftime("%Y-%m-%d %H:%M")
	templateData = {
		'title' : 'Trail Counter',
		'time': timeString,
		'wifi_count': str(serial_obj.get_wifi_count()),
		'bluetooth_count': str(serial_obj.get_bt_count())
		}
	return render_template('index.html', **templateData)

@app.route("/get_complete_log")
def get_complete_log():
	with open("/home/pi/rpiWebServer/logs/complete_log.csv") as fp:
		csv = fp.read()
	return Response(
		csv,
		mimetype="text/csv",
		headers={"Content-disposition":
				 "attachment; filename=complete_log.csv"})

@app.route("/get_summary_log")
def get_summary_log():
	with open("/home/pi/rpiWebServer/logs/summary_log.csv") as fp:
		csv = fp.read()
	return Response(
		csv,
		mimetype="text/csv",
		headers={"Content-disposition":
				 "attachment; filename=summary_log.csv"})

@app.route("/reset_logs")
def reset_logs():
	serial_obj.reset_detections()
	return index()

stopFlag = threading.Event()
serial_obj = serial_thread(stopFlag, '/dev/ttyUSB0')


if __name__ == "__main__":
	time.sleep(2.0)
	print("Opening Serial Port")
	wiringpi.wiringPiSetup()
	wiringpi.pinMode(12, 1)
	serial_obj.start()

	sched_thread = scheduler_thread(stopFlag, serial_obj)
	sched_thread.start()

	# warning: turning on debugging below restarts the application, leading to 2 serial port objects running simulatenously?!?!?
	app.run(host='0.0.0.0', port=80, debug=False) 
	stopFlag.set()
	serial_obj.serial_port.close()

