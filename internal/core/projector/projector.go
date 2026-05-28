// Package projector controls a projector via NEC IR commands through LIRC.
package projector

import (
	"encoding/binary"
	"log"
	"os"
	"time"

	"github.com/willheisenberg/KodiMediaBot/internal/config"
)

// Controller manages IR signal generation and transmission for the projector.
type Controller struct {
	DevicePath string
}

// Projector is the singleton instance.
var Projector = &Controller{DevicePath: "/dev/lirc0"}

// Connect verifies that the native LIRC device is available.
func (c *Controller) Connect() bool {
	if _, err := os.Stat(c.DevicePath); os.IsNotExist(err) {
		log.Printf("ERROR LIRC hardware device %s not found. "+
			"Please ensure that 'dtoverlay=gpio-ir-tx,gpio_pin=17' is enabled in "+
			"/flash/config.txt and the container has privileged access.", c.DevicePath)
		return false
	}
	return true
}

// SendCommand sends an IR command multiple times with pauses using LIRC.
func (c *Controller) SendCommand(address, command, repeatCount, delayMs int) bool {
	if !c.Connect() {
		return false
	}

	// Build LIRC raw pulses (microsecond durations)
	// Starts with header (9ms pulse, 4.5ms space)
	pulses := []uint32{9000, 4500}

	// 32-bit payload: address (8), ~address (8), command (8), ~command (8)
	invAddress := (^address) & 0xFF
	invCommand := (^command) & 0xFF
	payload := uint32(address) | (uint32(invAddress) << 8) | (uint32(command) << 16) | (uint32(invCommand) << 24)

	for i := 0; i < 32; i++ {
		bit := (payload >> uint(i)) & 1
		pulses = append(pulses, 560) // Pulse
		if bit == 1 {
			pulses = append(pulses, 1690) // Space
		} else {
			pulses = append(pulses, 560) // Space
		}
	}

	// Stop bit (560us pulse)
	pulses = append(pulses, 560)

	// Convert to binary 32-bit unsigned integers
	binaryData := make([]byte, len(pulses)*4)
	for i, p := range pulses {
		binary.LittleEndian.PutUint32(binaryData[i*4:], p)
	}

	log.Printf("INFO Transmitting NEC command via native LIRC interface (%s)...", c.DevicePath)
	for i := 0; i < repeatCount; i++ {
		if i > 0 {
			time.Sleep(time.Duration(delayMs) * time.Millisecond)
		}
		f, err := os.OpenFile(c.DevicePath, os.O_WRONLY, 0)
		if err != nil {
			log.Printf("ERROR LIRC open failed: %v", err)
			return false
		}
		_, err = f.Write(binaryData)
		f.Close()
		if err != nil {
			log.Printf("ERROR LIRC write failed: %v", err)
			return false
		}
	}
	return true
}

// PowerOn transmits the POWER_ON command with rapid burst repetitions.
func (c *Controller) PowerOn() bool {
	cfg := config.Get()
	log.Printf("INFO Sending Projector POWER_ON...")
	return c.SendCommand(cfg.ProjectorAddress, cfg.ProjectorPowerOnCode, cfg.ProjectorPowerOnRepeats, 40)
}

// PowerOff transmits the POWER_OFF command twice with a 1 second delay.
func (c *Controller) PowerOff() bool {
	cfg := config.Get()
	log.Printf("INFO Sending Projector POWER_OFF (Double Salve)...")
	ok1 := c.SendCommand(cfg.ProjectorAddress, cfg.ProjectorPowerOffCode, 4, 40)
	time.Sleep(1 * time.Second)
	ok2 := c.SendCommand(cfg.ProjectorAddress, cfg.ProjectorPowerOffCode, 4, 40)
	return ok1 && ok2
}
