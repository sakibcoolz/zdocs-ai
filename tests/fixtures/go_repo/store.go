package storage

import (
	"errors"
	"fmt"
)

// Store is the persistence contract.
type Store interface {
	Get(id string) (string, error)
	Save(id string, value string) error
}

// Auditor records what happened.
type Auditor interface {
	Record(event string)
}

// Base carries shared identity fields.
type Base struct {
	ID   string
	Kind string
}

// MemoryStore keeps everything in a map. It satisfies Store.
type MemoryStore struct {
	Base
	values  map[string]string
	auditor Auditor
}

// NewMemoryStore builds a MemoryStore with an injected auditor.
func NewMemoryStore(auditor Auditor) *MemoryStore {
	return &MemoryStore{values: map[string]string{}, auditor: auditor}
}

// Get returns a stored value.
func (m *MemoryStore) Get(id string) (string, error) {
	value, ok := m.values[id]
	if !ok {
		return "", errors.New("not found")
	}
	return value, nil
}

// Save writes a value.
func (m *MemoryStore) Save(id string, value string) error {
	m.values[id] = value
	m.auditor.Record(fmt.Sprintf("saved %s", id))
	return nil
}

// ReadOnlyStore only implements half of Store, so it must not be reported
// as an implementation.
type ReadOnlyStore struct {
	values map[string]string
}

// Get returns a stored value.
func (r *ReadOnlyStore) Get(id string) (string, error) {
	return r.values[id], nil
}
