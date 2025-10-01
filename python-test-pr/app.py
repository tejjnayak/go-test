import http.client

# Specify the host and path
host = "google.com"
path = "/"

# Create a connection to the host
connection = http.client.HTTPConnection(host)

# Send a GET request
connection.request("GET", path)

# Get the response
response = connection.getresponse()

# Read and print the content
if response.status == 200:
    content = response.read()
    print(content.decode('utf-8'))
else:
    print(f"Failed to fetch the page. Status code: {response.status}")

# Close the connection
connection.close()
