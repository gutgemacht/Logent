#!/bin/bash

# Display the current time, date, and show every letter in the output with different colors
echo "Current time: $(date '+%H:%M:%S')"

# Function to colorize every character in a string and print it
colorize_output() {
  local text="$1"
  local colors=(
    "\033[1;31m" "\033[1;32m" "\033[1;33m" "\033[1;34m"
    "\033[1;35m" "\033[1;35m" "\033[1;36m" "\033[1;37m"
  )
  local colors_count=${#colors[@]}
  local index=0

  for (( i=0; i<${#text}; i++ )); do
    local char="${text:$i:1}"
    local color="${colors[$((index % colors_count))]}"
    echo -e "${color}${char}\033[0m"
    ((index++))
  done
}

# Function to show the Linux version
show_linux_version() {
  echo "Linux version: $(uname -r)"
}

# Example usage: colorize and print a string
colorize_output "Hello World"

# Example usage: show the Linux version
show_linux_version

